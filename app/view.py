"""서버와 정적 빌드가 같이 쓰는 화면 설정."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

BASE = Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))

KINDS = {"news": "뉴스", "community": "커뮤니티", "video": "영상", "research": "연구"}
TABS = [("", "전체"), ("hot", "개념글"), *KINDS.items()]
HOT_MIN = 10  # 반응+댓글이 이만큼이면 개념글
PLATFORMS = {
    "hn": "Hacker News", "reddit": "Reddit", "lobsters": "Lobsters", "bsky": "Bluesky",
    "masto": "Mastodon", "devto": "dev.to", "youtube": "YouTube",
}


def fmt_time(ts: int) -> str:
    """오늘 글은 시:분, 그 외는 월.일 — 게시판 관례."""
    d = datetime.fromtimestamp(ts, KST)
    return d.strftime("%H:%M") if d.date() == datetime.now(KST).date() else d.strftime("%m.%d")


def fmt_full(ts: int) -> str:
    return datetime.fromtimestamp(ts, KST).strftime("%Y.%m.%d %H:%M")


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=BASE / "templates")
    templates.env.filters["t"] = fmt_time
    templates.env.filters["tfull"] = fmt_full
    templates.env.globals.update(PLATFORMS=PLATFORMS, KINDS=KINDS, TABS=TABS)
    return templates
