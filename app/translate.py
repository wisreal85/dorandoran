"""제목·요약·반응을 한국어로 옮긴다.

GitHub Models(GitHub 계정에 딸린 무료 AI 모델)를 쓴다. GitHub Actions 안에서는
워크플로의 GITHUB_TOKEN(permissions: models: read)으로 키 없이 돈다.
무료 한도가 작아서 여러 건을 한 요청에 묶고, 회차마다 요청 수를 제한한다.
한도에 걸리면 그 회차는 멈추고 다음 회차에 이어서 옮긴다.

수동 실행:  GITHUB_TOKEN=... python -m app.translate
"""
import asyncio
import json
import logging
import os
import re

import httpx

from .db import db, init

log = logging.getLogger("doran.translate")

ENDPOINT = "https://models.github.ai/inference/chat/completions"
MODELS = [m for m in os.environ.get("DORAN_TR_MODELS", "openai/gpt-4.1-mini,openai/gpt-4o-mini").split(",") if m]
MAX_REQUESTS = int(os.environ.get("DORAN_TR_REQUESTS", "8"))  # 회차당 요청 수
POSTS_PER_REQ = 12
RX_PER_REQ = 25

_HANGUL = re.compile(r"[가-힣]")


def is_korean(text: str) -> bool:
    letters = re.sub(r"[\W\d_]", "", text or "")
    return bool(letters) and len(_HANGUL.findall(letters)) / len(letters) > 0.3


POST_PROMPT = """너는 한국 IT 커뮤니티 게시판에 해외 AI 소식을 퍼 나르는 사람이다.
입력의 각 항목(title, text)을 보고 한국어로 다음을 만든다.
- title: 게시판 제목. 원제의 뜻을 살린 자연스러운 한국어 한 줄. 낚시성 과장 금지.
- summary: 무슨 일인지 2~3문장 요약. text에 없는 사실은 지어내지 말 것. text가 비어 있으면 제목만으로 짧게 한 문장.
고유명사(회사·제품·모델명)는 원문 표기를 유지한다.
반드시 {"items":[{"id":..,"title":"..","summary":".."}]} 형태의 JSON만 출력한다."""

RX_PROMPT = """너는 해외 커뮤니티 댓글을 한국 커뮤니티 말투로 옮기는 번역가다.
각 항목의 text를 자연스러운 한국어로 옮긴다. 반말 커뮤니티체(~함, ~임, ~인 듯)를 쓰되 뜻을 바꾸거나 덧붙이지 않는다.
욕설은 순화하지 말고 그대로 옮기되 혐오 표현은 [순화됨]으로 바꾼다. 코드·링크·고유명사는 그대로 둔다.
반드시 {"items":[{"id":..,"text":".."}]} 형태의 JSON만 출력한다."""


class OutOfQuota(Exception):
    pass


class Translator:
    def __init__(self, client: httpx.AsyncClient, token: str):
        self.client, self.token = client, token
        self.left = MAX_REQUESTS
        self.models = list(MODELS)

    async def ask(self, system: str, items: list[dict]) -> dict:
        if self.left <= 0:
            raise OutOfQuota("이번 회차 요청 수를 다 씀")
        while self.models:
            self.left -= 1
            r = await self.client.post(ENDPOINT, headers={"Authorization": f"Bearer {self.token}"}, json={
                "model": self.models[0],
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": json.dumps({"items": items}, ensure_ascii=False)}],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            })
            limits = {k: v for k, v in r.headers.items() if "ratelimit" in k.lower()}
            if r.status_code == 429:
                log.warning("%s 한도 초과 %s", self.models[0], limits)
                self.models.pop(0)  # 다음 모델로 넘어간다 (모델마다 한도가 따로)
                if self.left <= 0:
                    break
                continue
            r.raise_for_status()
            log.info("번역 %s %d건 %s", self.models[0], len(items), limits)
            content = r.json()["choices"][0]["message"]["content"]
            return {str(it.get("id")): it for it in json.loads(content).get("items", [])}
        raise OutOfQuota("모든 모델이 한도에 걸림")


def keep_korean() -> None:
    """원래 한국어인 글·반응은 번역 없이 그대로 쓴다."""
    with db() as conn:
        for r in conn.execute("SELECT id, title FROM posts WHERE title_ko IS NULL").fetchall():
            if is_korean(r["title"]):
                conn.execute("UPDATE posts SET title_ko = title, summary_ko = summary WHERE id = ?", (r["id"],))
        for r in conn.execute("SELECT id, body FROM reactions WHERE body_ko IS NULL").fetchall():
            if is_korean(r["body"]):
                conn.execute("UPDATE reactions SET body_ko = body WHERE id = ?", (r["id"],))


async def translate_posts(tr: Translator) -> int:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, title, summary FROM posts WHERE title_ko IS NULL ORDER BY published_at DESC LIMIT 200").fetchall()
    done = 0
    for i in range(0, len(rows), POSTS_PER_REQ):
        chunk = rows[i:i + POSTS_PER_REQ]
        got = await tr.ask(POST_PROMPT, [{"id": r["id"], "title": r["title"], "text": r["summary"]} for r in chunk])
        with db() as conn:
            for r in chunk:
                it = got.get(str(r["id"]))
                if it and it.get("title"):
                    conn.execute("UPDATE posts SET title_ko = ?, summary_ko = ? WHERE id = ?",
                                 (it["title"].strip()[:300], (it.get("summary") or "").strip()[:800], r["id"]))
                    done += 1
    return done


async def translate_reactions(tr: Translator) -> int:
    with db() as conn:
        # 최근 글, 반응 많은 글부터
        rows = conn.execute(
            "SELECT r.id, r.body FROM reactions r JOIN posts p ON p.id = r.post_id WHERE r.body_ko IS NULL"
            " ORDER BY p.published_at DESC, r.created_at LIMIT 600").fetchall()
    done = 0
    for i in range(0, len(rows), RX_PER_REQ):
        chunk = rows[i:i + RX_PER_REQ]
        got = await tr.ask(RX_PROMPT, [{"id": r["id"], "text": r["body"]} for r in chunk])
        with db() as conn:
            for r in chunk:
                it = got.get(str(r["id"]))
                if it and it.get("text"):
                    conn.execute("UPDATE reactions SET body_ko = ? WHERE id = ?", (it["text"].strip()[:1200], r["id"]))
                    done += 1
    return done


async def translate_once() -> dict:
    init()
    keep_korean()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    result = {"글": 0, "반응": 0}
    async with httpx.AsyncClient(timeout=120) as client:
        tr = Translator(client, token or "")
        try:
            if not token:
                raise OutOfQuota("GITHUB_TOKEN 없음, 한국어 원문만 정리함")
            result["글"] = await translate_posts(tr)
            result["반응"] = await translate_reactions(tr)
        except OutOfQuota as e:
            log.info("번역 멈춤: %s", e)
        except Exception:
            log.exception("번역 오류")
    with db() as conn:
        left = conn.execute("SELECT (SELECT COUNT(*) FROM posts WHERE title_ko IS NULL),"
                            " (SELECT COUNT(*) FROM reactions WHERE body_ko IS NULL)").fetchone()
    result["남은 글"], result["남은 반응"] = left[0], left[1]
    log.info("번역 완료 %s", result)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(translate_once())
