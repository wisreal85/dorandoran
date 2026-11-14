"""제목·요약·반응을 한국어로 옮긴다.

외부 API 없이 오픈소스 번역 모델(Meta NLLB-200 distilled 600M, CTranslate2 int8)을
GitHub Actions 러너의 CPU에서 직접 돌린다. 키·요금 없음.
회차마다 시간 예산 안에서 최근 글부터 옮기고, 남은 것은 다음 회차에 이어서 한다.

수동 실행:  python -m app.translate      (모델이 없으면 처음에 약 600MB를 내려받음)
"""
import logging
import os
import re
import time
from pathlib import Path

import httpx

from .db import db, init

log = logging.getLogger("doran.translate")

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = Path(os.environ.get("DORAN_MODEL_DIR", ROOT / "models" / "nllb-600m"))
MODEL_REPO = "JustFrederik/nllb-200-distilled-600M-ct2-int8"
MODEL_FILES = ["model.bin", "sentencepiece.bpe.model", "shared_vocabulary.txt", "config.json"]
TIME_BUDGET = int(os.environ.get("DORAN_TR_SECONDS", "480"))  # 회차당 번역에 쓰는 시간
BATCH = 16

_HANGUL = re.compile(r"[가-힣]")
_KANA = re.compile(r"[぀-ヿ]")
_SENT = re.compile(r"(?<=[.!?。！？])\s+")


def is_korean(text: str) -> bool:
    letters = re.sub(r"[\W\d_]", "", text or "")
    return bool(letters) and len(_HANGUL.findall(letters)) / len(letters) > 0.3


def src_lang(text: str) -> str:
    return "jpn_Jpan" if _KANA.search(text) else "eng_Latn"


def ensure_model() -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name in MODEL_FILES:
        path = MODEL_DIR / name
        if path.exists() and path.stat().st_size > 0:
            continue
        log.info("번역 모델 내려받는 중: %s", name)
        tmp = path.with_suffix(path.suffix + ".part")
        with httpx.stream("GET", f"https://huggingface.co/{MODEL_REPO}/resolve/main/{name}",
                          follow_redirects=True, timeout=600) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        tmp.replace(path)


class Nllb:
    def __init__(self):
        import ctranslate2
        import sentencepiece as spm
        ensure_model()
        self.tr = ctranslate2.Translator(str(MODEL_DIR), device="cpu", compute_type="int8",
                                         inter_threads=1, intra_threads=os.cpu_count() or 2)
        self.sp = spm.SentencePieceProcessor(model_file=str(MODEL_DIR / "sentencepiece.bpe.model"))

    def _run(self, sentences: list[str]) -> list[str]:
        out = []
        for i in range(0, len(sentences), BATCH):
            chunk = sentences[i:i + BATCH]
            src = [self.sp.encode(s, out_type=str)[:400] + ["</s>", src_lang(s)] for s in chunk]
            res = self.tr.translate_batch(src, target_prefix=[["kor_Hang"]] * len(chunk), beam_size=2,
                                          max_decoding_length=320, repetition_penalty=1.1)
            out += [re.sub(r"\s*⁇\s*", " ", self.sp.decode(r.hypotheses[0][1:])).strip() for r in res]
        return out

    def translate(self, texts: list[str]) -> list[str]:
        """여러 문장으로 된 글은 문장 단위로 쪼개 옮긴다 (통째로 넣으면 뒷문장을 빠뜨리는 버릇이 있음)."""
        pieces, owner = [], []
        for i, t in enumerate(texts):
            for s in _SENT.split(t.strip()):
                if s:
                    pieces.append(s)
                    owner.append(i)
        done = self._run(pieces)
        joined = [[] for _ in texts]
        for i, s in zip(owner, done):
            joined[i].append(s)
        return [" ".join(j) for j in joined]


def keep_korean() -> None:
    """원래 한국어인 글·반응은 번역 없이 그대로 쓴다."""
    with db() as conn:
        for r in conn.execute("SELECT id, title FROM posts WHERE title_ko IS NULL").fetchall():
            if is_korean(r["title"]):
                conn.execute("UPDATE posts SET title_ko = title, summary_ko = summary WHERE id = ?", (r["id"],))
        for r in conn.execute("SELECT id, body FROM reactions WHERE body_ko IS NULL").fetchall():
            if is_korean(r["body"]):
                conn.execute("UPDATE reactions SET body_ko = body WHERE id = ?", (r["id"],))


def translate_once() -> dict:
    init()
    keep_korean()
    deadline = time.monotonic() + TIME_BUDGET
    result = {"글": 0, "반응": 0}
    with db() as conn:
        posts = conn.execute("SELECT id, title, summary FROM posts WHERE title_ko IS NULL"
                             " ORDER BY published_at DESC").fetchall()
        rxs = conn.execute(
            "SELECT r.id, r.body FROM reactions r JOIN posts p ON p.id = r.post_id WHERE r.body_ko IS NULL"
            " ORDER BY p.published_at DESC, r.created_at").fetchall()
    if posts or rxs:
        nllb = Nllb()
        # 목록에 보이는 제목이 먼저, 그다음 반응
        for i in range(0, len(posts), BATCH):
            if time.monotonic() > deadline:
                break
            chunk = posts[i:i + BATCH]
            titles = nllb.translate([r["title"] for r in chunk])
            sums = nllb.translate([r["summary"] or "" for r in chunk])
            with db() as conn:
                for r, t, s in zip(chunk, titles, sums):
                    conn.execute("UPDATE posts SET title_ko = ?, summary_ko = ? WHERE id = ?", (t[:300], s[:1200], r["id"]))
            result["글"] += len(chunk)
        for i in range(0, len(rxs), BATCH):
            if time.monotonic() > deadline:
                break
            chunk = rxs[i:i + BATCH]
            bodies = nllb.translate([r["body"] for r in chunk])
            with db() as conn:
                for r, b in zip(chunk, bodies):
                    conn.execute("UPDATE reactions SET body_ko = ? WHERE id = ?", (b[:1500], r["id"]))
            result["반응"] += len(chunk)
    with db() as conn:
        left = conn.execute("SELECT (SELECT COUNT(*) FROM posts WHERE title_ko IS NULL),"
                            " (SELECT COUNT(*) FROM reactions WHERE body_ko IS NULL)").fetchone()
    result["남은 글"], result["남은 반응"] = left[0], left[1]
    log.info("번역 완료 %s", result)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    translate_once()
