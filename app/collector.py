"""AI 생태계 소식과 바깥 반응을 주기적으로 긁어오는 수집기.

키·결제 없이 무료로 열려 있는 통로만 쓴다 (RSS, 공개 API).
  글:   뉴스·블로그 RSS, Hacker News, Reddit RSS, Lobsters, GeekNews, dev.to,
        유튜브 채널 RSS, Hugging Face 논문·모델, GitHub 신규 인기 저장소
  반응: Hacker News 댓글, Reddit 댓글, Lobsters 댓글, dev.to 댓글,
        Bluesky(원문 주소 검색), Mastodon(태그 타임라인에서 원문 주소 매칭)

수동 실행:  python -m app.collector
"""
import asyncio
import calendar
import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from .db import db, init

log = logging.getLogger("doran.collector")
logging.getLogger("httpx").setLevel(logging.WARNING)  # 요청마다 찍히는 로그는 끈다

UA = {"User-Agent": "dorandoran-collector/0.2 (AI news board; RSS and public APIs only)"}
KEEP_DAYS = 14
MAX_RX = 30  # 글 하나당 플랫폼별 반응 상한

# (퍼온 곳, 피드 주소, AI 전용 여부) — 전용이 아니면 제목·요약에 AI 키워드가 있을 때만 담는다
NEWS_FEEDS = [
    ("TechCrunch", "https://techcrunch.com/category/artificial-intelligence/feed/", True),
    ("The Verge", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", True),
    ("Ars Technica", "https://arstechnica.com/ai/feed/", True),
    ("MIT Tech Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed", True),
    ("WIRED", "https://www.wired.com/feed/tag/ai/latest/rss", True),
    ("The Decoder", "https://the-decoder.com/feed/", True),
    ("The Register", "https://www.theregister.com/software/ai_ml/headlines.atom", True),
    ("The Guardian", "https://www.theguardian.com/technology/artificialintelligenceai/rss", True),
    ("MarkTechPost", "https://www.marktechpost.com/feed/", True),
    ("404 Media", "https://www.404media.co/rss/", False),
    ("OpenAI", "https://openai.com/news/rss.xml", True),
    ("Google AI", "https://blog.google/technology/ai/rss/", True),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml", True),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml", True),
    ("NVIDIA", "https://blogs.nvidia.com/feed/", False),
    ("AWS ML", "https://aws.amazon.com/blogs/machine-learning/feed/", True),
    ("Simon Willison", "https://simonwillison.net/atom/everything/", False),
    ("Latent Space", "https://www.latent.space/feed", True),
    ("Import AI", "https://importai.substack.com/feed", True),
    ("AI타임스", "https://www.aitimes.com/rss/allArticle.xml", True),
    ("인공지능신문", "https://www.aitimes.kr/rss/allArticle.xml", True),
    ("ZDNet Korea", "https://feeds.feedburner.com/zdkorea", False),
    ("블로터", "https://www.bloter.net/rss/allArticle.xml", False),
]
COMMUNITY_FEEDS = [
    ("GeekNews", "https://news.hada.io/rss/news", False),
    ("파이토치 한국", "https://discuss.pytorch.kr/latest.rss", True),
]
# 유튜브 채널 RSS (키 불필요). 퍼온 곳 이름은 피드가 알려주는 채널명을 쓴다.
YOUTUBE_CHANNELS = [
    "UCbfYPyITQ-7l4upoX8nvctg", "UCXZCJLdBC09xxGZ6gcdrc6A", "UCP7jMXSY2xbc3KCAE0MHQ-A",
    "UCrDwWp7EBBv4NwvScIpBDOA",
    "UCawZsQWqfGSbCI5yjkdVkTA", "UCXUPKJO5MZQN11PqgIvyuvQ", "UCsBjURrPoezykLs9EqgamOA",
]
SUBREDDITS = ["LocalLLaMA", "MachineLearning", "OpenAI", "ClaudeAI", "singularity", "artificial", "StableDiffusion"]
HN_QUERIES = ["AI", "LLM", "OpenAI", "Anthropic", "Claude", "GPT", "Gemini", "agent"]
HN_MIN_POINTS = 80
HN_API = "https://hn.algolia.com/api/v1"
MASTODON_TAGS = ["ai", "llm", "openai", "genai", "machinelearning"]

AI_WORDS = re.compile(
    r"\bAI\b|A\.I\.|\b(LLMs?|GPT|ChatGPT|OpenAI|Anthropic|Claude|Gemini|DeepMind|Llama|Mistral|Copilot|"
    r"agents?|agentic|machine learning|neural|diffusion|transformers?|Nvidia|Hugging ?Face|RAG|MCP)\b|"
    r"인공지능|생성형|챗GPT|에이전트|머신러닝|딥러닝|오픈AI|엔비디아|제미나이|클로드|거대언어|언어모델",
    re.I,
)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_URL = re.compile(r"https?://\S+")


def now() -> int:
    return int(time.time())


def clean(text: str, limit: int = 0) -> str:
    """HTML을 걷어내고 한 줄 텍스트로 만든다."""
    text = re.sub(r"<(p|br|li|div)\b[^>]*>", " ", text or "", flags=re.I)
    text = _WS.sub(" ", html.unescape(_TAG.sub("", text))).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def iso(s: str) -> int:
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return now()


def norm_url(u: str) -> str:
    u = re.sub(r"[?#].*$", "", u or "").rstrip("/").lower()
    return re.sub(r"^https?://(www\.)?", "", u)


def entry_time(e) -> int:
    t = e.get("published_parsed") or e.get("updated_parsed")
    return min(calendar.timegm(t), now()) if t else now()


def add_post(conn, *, title, url, source, kind, published_at, summary="", origin="rss", origin_id=None, hn_id=None) -> int:
    if not title or not url or published_at < now() - KEEP_DAYS * 86400:
        return 0
    cur = conn.execute(
        "INSERT OR IGNORE INTO posts(title, summary, url, source, kind, origin, origin_id, hn_id, published_at, collected_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (title[:300], summary, url, source, kind, origin, origin_id, hn_id, published_at, now()),
    )
    return cur.rowcount


def add_reaction(conn, post_id, platform, ext_id, author, body, url, created_at) -> int:
    body = clean(body, 600)
    if len(_URL.sub("", body).strip()) < 8:
        return 0
    cur = conn.execute(
        "INSERT OR IGNORE INTO reactions(post_id, platform, ext_id, author, body, url, created_at) VALUES (?,?,?,?,?,?,?)",
        (post_id, platform, str(ext_id), author or "", body, url or "", created_at),
    )
    return cur.rowcount


async def get(client, url, **params):
    r = await client.get(url, params=params or None)
    r.raise_for_status()
    return r


def pending(origin_clause: str, column: str, max_age_days: float, recheck_hours: float, limit: int):
    """반응을 다시 확인할 차례인 글을 고른다."""
    with db() as conn:
        return conn.execute(
            f"SELECT * FROM posts WHERE {origin_clause} AND published_at > ? AND {column} < ?"
            " ORDER BY published_at DESC LIMIT ?",
            (now() - int(max_age_days * 86400), now() - int(recheck_hours * 3600), limit),
        ).fetchall()


def mark(post_id: int, column: str) -> None:
    with db() as conn:
        conn.execute(f"UPDATE posts SET {column} = ? WHERE id = ?", (now(), post_id))


# ───────────────────────── 글 수집 ─────────────────────────

async def collect_feeds(client, feeds, kind) -> int:
    async def one(source, url, dedicated):
        try:
            feed = feedparser.parse((await get(client, url)).content)
        except Exception as e:
            log.warning("피드 실패 %s: %s", source, e)
            return 0
        n = 0
        with db() as conn:
            for e in feed.entries[:30]:
                title, summary = clean(e.get("title", "")), clean(e.get("summary", ""), 400)
                if not dedicated and not AI_WORDS.search(f"{title} {summary}"):
                    continue
                n += add_post(conn, title=title, summary=summary, url=e.get("link"), source=source,
                              kind=kind, published_at=entry_time(e))
        return n
    return sum(await asyncio.gather(*(one(*f) for f in feeds)))


async def collect_youtube(client) -> int:
    n = 0
    for cid in YOUTUBE_CHANNELS:
        try:
            feed = feedparser.parse((await get(client, "https://www.youtube.com/feeds/videos.xml", channel_id=cid)).content)
        except Exception as e:
            log.warning("유튜브 실패 %s: %s", cid, e)
            continue
        source = clean(feed.feed.get("title", "")) or "YouTube"
        with db() as conn:
            for e in feed.entries[:8]:
                n += add_post(conn, title=clean(e.get("title", "")), summary=clean(e.get("summary", ""), 400),
                              url=e.get("link"), source=source, kind="video", origin="youtube",
                              origin_id=e.get("yt_videoid"), published_at=entry_time(e))
    return n


async def collect_hn(client) -> int:
    n, seen, since = 0, set(), now() - 2 * 86400
    for q in HN_QUERIES:
        try:
            hits = (await get(client, f"{HN_API}/search_by_date", query=q, tags="story", hitsPerPage=30,
                              numericFilters=f"points>{HN_MIN_POINTS},created_at_i>{since}")).json().get("hits", [])
        except Exception as e:
            log.warning("HN 검색 실패 %s: %s", q, e)
            continue
        with db() as conn:
            for h in hits:
                hn_id, title = str(h["objectID"]), clean(h.get("title", ""))
                if hn_id in seen or not AI_WORDS.search(title):  # 본문만 걸린 무관한 글은 버린다
                    continue
                seen.add(hn_id)
                url = h.get("url") or f"https://news.ycombinator.com/item?id={hn_id}"
                row = conn.execute("SELECT id FROM posts WHERE url = ?", (url,)).fetchone()
                if row:  # 이미 뉴스로 들어온 글이면 토론만 연결
                    conn.execute("UPDATE posts SET hn_id = ? WHERE id = ? AND hn_id IS NULL", (hn_id, row["id"]))
                    continue
                n += add_post(conn, title=title, summary=clean(h.get("story_text") or "", 400), url=url,
                              source="Hacker News", kind="community", origin="hn", origin_id=hn_id, hn_id=hn_id,
                              published_at=h["created_at_i"])
    return n


async def collect_reddit(client) -> int:
    n = 0
    start = (now() // 1800) % len(SUBREDDITS)  # 회차마다 시작 서브레딧을 돌려 한도를 나눠 쓴다
    for sub in SUBREDDITS[start:] + SUBREDDITS[:start]:
        try:
            feed = feedparser.parse((await get(client, f"https://www.reddit.com/r/{sub}/top/.rss", t="day", limit=20)).content)
        except Exception as e:
            log.warning("Reddit 실패 r/%s: %s", sub, e)
            break  # 한도에 걸리면 이번 회차는 그만, 다음 회차에 이어서
        with db() as conn:
            for e in feed.entries:
                rid = (e.get("id") or "").replace("t3_", "")
                body = e.get("content", [{}])[0].get("value", "") if e.get("content") else e.get("summary", "")
                summary = re.sub(r"\s*submitted by\s+/u/\S+.*$", "", clean(body), flags=re.S)
                if len(summary) > 400:
                    summary = summary[:400].rsplit(" ", 1)[0] + "…"
                n += add_post(conn, title=clean(e.get("title", "")), summary=summary, url=e.get("link"),
                              source=f"r/{sub}", kind="community", origin="reddit", origin_id=rid,
                              published_at=entry_time(e))
        await asyncio.sleep(6)  # Reddit은 느긋하게
    return n


async def collect_lobsters(client) -> int:
    try:
        items = (await get(client, "https://lobste.rs/t/ai.json")).json()
    except Exception as e:
        log.warning("Lobsters 실패: %s", e)
        return 0
    n = 0
    with db() as conn:
        for s in items:
            n += add_post(conn, title=clean(s.get("title", "")), summary=clean(s.get("description_plain", ""), 400),
                          url=s.get("url") or s.get("comments_url"), source="Lobsters", kind="community",
                          origin="lobsters", origin_id=s.get("short_id"), published_at=iso(s.get("created_at", "")))
    return n


async def collect_devto(client) -> int:
    n = 0
    for tag in ("ai", "llm", "machinelearning"):
        try:
            items = (await get(client, "https://dev.to/api/articles", tag=tag, top=2, per_page=15)).json()
        except Exception as e:
            log.warning("dev.to 실패 %s: %s", tag, e)
            continue
        with db() as conn:
            for a in items:
                n += add_post(conn, title=clean(a.get("title", "")), summary=clean(a.get("description", ""), 400),
                              url=a.get("url"), source="dev.to", kind="community", origin="devto",
                              origin_id=str(a.get("id")), published_at=iso(a.get("published_timestamp") or ""))
    return n


async def collect_research(client) -> int:
    n = 0
    try:  # Hugging Face가 매일 고르는 논문
        for p in (await get(client, "https://huggingface.co/api/daily_papers", limit=40)).json():
            paper = p.get("paper", {})
            with db() as conn:
                n += add_post(conn, title=clean(p.get("title") or paper.get("title", "")),
                              summary=clean(paper.get("summary", ""), 400),
                              url=f"https://huggingface.co/papers/{paper.get('id')}", source="HF 논문", kind="research",
                              origin="hf", origin_id=paper.get("id"), published_at=iso(p.get("publishedAt", "")))
    except Exception as e:
        log.warning("HF 논문 실패: %s", e)
    try:  # 지금 뜨는 모델
        for m in (await get(client, "https://huggingface.co/api/models", sort="trendingScore", limit=20)).json():
            mid = m.get("id") or m.get("modelId")
            bits = [m.get("pipeline_tag") or "", f"좋아요 {m.get('likes', 0)}", f"다운로드 {m.get('downloads', 0)}"]
            with db() as conn:
                n += add_post(conn, title=f"[모델] {mid}", summary=" · ".join(b for b in bits if b),
                              url=f"https://huggingface.co/{mid}", source="HF 모델", kind="research", origin="hf",
                              origin_id=mid, published_at=max(iso(m.get("createdAt", "")), now() - 3 * 86400))
    except Exception as e:
        log.warning("HF 모델 실패: %s", e)
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    for topic in ("llm", "ai-agents"):  # 최근 일주일에 생겨 별을 많이 받은 저장소
        try:
            items = (await get(client, "https://api.github.com/search/repositories",
                               q=f"topic:{topic} created:>{since}", sort="stars", per_page=15)).json().get("items", [])
        except Exception as e:
            log.warning("GitHub 실패 %s: %s", topic, e)
            continue
        with db() as conn:
            for r in items:
                if r.get("stargazers_count", 0) < 30:
                    continue
                n += add_post(conn, title=f"[저장소] {r['full_name']}",
                              summary=clean(f"{r.get('description') or ''} · 별 {r['stargazers_count']}", 400),
                              url=r["html_url"], source="GitHub", kind="research", origin="github",
                              origin_id=r["full_name"], published_at=iso(r.get("created_at", "")))
    return n


# ───────────────────────── 반응 수집 ─────────────────────────

async def link_hn(client) -> None:
    """원문 주소로 HN 토론이 있는지 찾아 연결한다."""
    for row in pending("hn_id IS NULL AND kind IN ('news','research')", "hn_checked_at", 3, 4, 40):
        hn_id = None
        try:
            hits = (await get(client, f"{HN_API}/search", query=row["url"], restrictSearchableAttributes="url",
                              tags="story", hitsPerPage=3)).json().get("hits", [])
            hits = [h for h in hits if (h.get("num_comments") or 0) > 0]
            if hits:
                hn_id = str(max(hits, key=lambda h: h.get("num_comments") or 0)["objectID"])
        except Exception as e:
            log.warning("HN 연결 실패: %s", e)
            continue
        with db() as conn:
            conn.execute("UPDATE posts SET hn_id = COALESCE(?, hn_id), hn_checked_at = ? WHERE id = ?",
                         (hn_id, now(), row["id"]))
        await asyncio.sleep(0.15)


async def rx_hn(client) -> int:
    n = 0
    for row in pending("hn_id IS NOT NULL", "rx_checked_at", 3, 1, 80):
        try:
            hits = (await get(client, f"{HN_API}/search", tags=f"comment,story_{row['hn_id']}",
                              hitsPerPage=MAX_RX)).json().get("hits", [])
        except Exception as e:
            log.warning("HN 반응 실패: %s", e)
            continue
        with db() as conn:
            for h in hits:
                n += add_reaction(conn, row["id"], "hn", h["objectID"], h.get("author"), h.get("comment_text") or "",
                                  f"https://news.ycombinator.com/item?id={h['objectID']}", h["created_at_i"])
        if row["origin"] == "hn":
            mark(row["id"], "rx_checked_at")
        await asyncio.sleep(0.15)
    return n


async def rx_reddit(client) -> int:
    n = 0
    for row in pending("origin = 'reddit'", "rx_checked_at", 2, 3, 6):
        try:
            feed = feedparser.parse((await get(client, f"https://www.reddit.com/comments/{row['origin_id']}/.rss",
                                               limit=MAX_RX)).content)
        except Exception as e:
            log.warning("Reddit 반응 실패: %s", e)
            break  # 막히면 이번 회차는 그만
        with db() as conn:
            for e in feed.entries:
                if not (e.get("id") or "").startswith("t1_"):
                    continue
                body = e.get("content", [{}])[0].get("value", "") if e.get("content") else ""
                n += add_reaction(conn, row["id"], "reddit", e["id"], (e.get("author") or "").replace("/u/", ""),
                                  body, e.get("link"), entry_time(e))
        mark(row["id"], "rx_checked_at")
        await asyncio.sleep(6)
    return n


async def rx_lobsters(client) -> int:
    n = 0
    for row in pending("origin = 'lobsters'", "rx_checked_at", 4, 3, 15):
        try:
            story = (await get(client, f"https://lobste.rs/s/{row['origin_id']}.json")).json()
        except Exception as e:
            log.warning("Lobsters 반응 실패: %s", e)
            continue
        with db() as conn:
            for c in story.get("comments", [])[:MAX_RX]:
                user = c.get("commenting_user")
                user = user.get("username") if isinstance(user, dict) else user
                n += add_reaction(conn, row["id"], "lobsters", c.get("short_id"), user,
                                  c.get("comment_plain") or c.get("comment") or "", c.get("url"), iso(c.get("created_at", "")))
        mark(row["id"], "rx_checked_at")
        await asyncio.sleep(0.5)
    return n


async def rx_devto(client) -> int:
    n = 0
    for row in pending("origin = 'devto'", "rx_checked_at", 4, 6, 15):
        try:
            items = (await get(client, "https://dev.to/api/comments", a_id=row["origin_id"])).json()
        except Exception as e:
            log.warning("dev.to 반응 실패: %s", e)
            continue
        with db() as conn:
            for c in items[:MAX_RX]:
                user = c.get("user") or {}
                n += add_reaction(conn, row["id"], "devto", c.get("id_code"), user.get("username") or user.get("name"),
                                  c.get("body_html") or "", f"https://dev.to/comment/{c.get('id_code')}",
                                  iso(c.get("created_at", "")))
        mark(row["id"], "rx_checked_at")
        await asyncio.sleep(0.5)
    return n


async def rx_bluesky(client) -> int:
    """원문 주소를 언급한 Bluesky 글을 찾는다."""
    n = 0
    for row in pending("origin IN ('rss','hn','youtube')", "bsky_checked_at", 2, 3, 50):
        try:
            posts = (await get(client, "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts",
                               q=row["url"], limit=25)).json().get("posts", [])
        except Exception as e:
            log.warning("Bluesky 실패: %s", e)
            break
        with db() as conn:
            for p in posts:
                handle, rkey = p.get("author", {}).get("handle", ""), p.get("uri", "").rsplit("/", 1)[-1]
                rec = p.get("record", {})
                n += add_reaction(conn, row["id"], "bsky", p.get("uri"), handle, rec.get("text", ""),
                                  f"https://bsky.app/profile/{handle}/post/{rkey}", iso(rec.get("createdAt", "")))
        mark(row["id"], "bsky_checked_at")
        await asyncio.sleep(0.3)
    return n


async def rx_mastodon(client) -> int:
    """AI 태그 타임라인에서 우리 글의 원문 주소를 공유한 툿을 찾는다."""
    with db() as conn:
        index = {norm_url(r["url"]): r["id"] for r in conn.execute(
            "SELECT id, url FROM posts WHERE published_at > ?", (now() - 4 * 86400,))}
    n = 0
    for tag in MASTODON_TAGS:
        try:
            statuses = (await get(client, f"https://mastodon.social/api/v1/timelines/tag/{tag}", limit=40)).json()
        except Exception as e:
            log.warning("Mastodon 실패 %s: %s", tag, e)
            continue
        with db() as conn:
            for s in statuses:
                post_id = index.get(norm_url((s.get("card") or {}).get("url", "")))
                if post_id:
                    n += add_reaction(conn, post_id, "masto", s.get("uri") or s.get("id"),
                                      "@" + s.get("account", {}).get("acct", ""), s.get("content", ""),
                                      s.get("url"), iso(s.get("created_at", "")))
    return n


def prune() -> None:
    with db() as conn:
        conn.execute("DELETE FROM posts WHERE published_at < ? AND id NOT IN (SELECT post_id FROM comments)",
                     (now() - KEEP_DAYS * 86400,))


async def collect_once() -> dict:
    init()
    result = {}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=UA) as client:
        steps = [
            ("뉴스", collect_feeds(client, NEWS_FEEDS, "news")),
            ("커뮤니티피드", collect_feeds(client, COMMUNITY_FEEDS, "community")),
            ("HN", collect_hn(client)),
            ("Reddit", collect_reddit(client)),
            ("Lobsters", collect_lobsters(client)),
            ("dev.to", collect_devto(client)),
            ("유튜브", collect_youtube(client)),
            ("연구", collect_research(client)),
            ("HN연결", link_hn(client)),
            ("반응:HN", rx_hn(client)),
            ("반응:Reddit", rx_reddit(client)),
            ("반응:Lobsters", rx_lobsters(client)),
            ("반응:dev.to", rx_devto(client)),
            ("반응:Bluesky", rx_bluesky(client)),
            ("반응:Mastodon", rx_mastodon(client)),
        ]
        for name, step in steps:  # 한 통로가 죽어도 나머지는 계속 돈다
            try:
                result[name] = await step
            except Exception as e:
                log.exception("%s 단계 오류", name)
                result[name] = f"오류: {e}"
    prune()
    log.info("수집 완료 %s", result)
    return result


async def run_forever(interval_min: int) -> None:
    while True:
        try:
            await collect_once()
        except Exception:
            log.exception("수집 중 오류")
        await asyncio.sleep(interval_min * 60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(collect_once())
