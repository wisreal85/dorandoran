"""도란도란 — AI 생태계 소식을 퍼 나르는 게시판.

실행:  uvicorn app.main:app --reload
"""
import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import urlencode
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import collector
from .view import KINDS, make_templates
from .db import db, init

BASE = Path(__file__).resolve().parent
PAGE_SIZE = 30
COLLECT_INTERVAL_MIN = int(os.environ.get("DORAN_COLLECT_MIN", "15"))  # 0이면 자동 수집 끔
COMMENT_COOLDOWN_SEC = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init()
    task = asyncio.create_task(collector.run_forever(COLLECT_INTERVAL_MIN)) if COLLECT_INTERVAL_MIN > 0 else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="도란도란", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = make_templates()


class ServerUrls:
    """서버로 띄웠을 때의 주소 규칙."""
    static = False
    css = "/static/style.css"
    privacy = "/privacy"

    def __init__(self, q: str = ""):
        self.q = q

    def board(self, kind: str = "", page: int = 1) -> str:
        return "/?" + urlencode({"kind": kind, "q": self.q, "page": page})

    def post(self, post_id: int) -> str:
        return f"/post/{post_id}"


def hash_pw(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${digest}"


def check_pw(pw: str, stored: str) -> bool:
    salt = stored.split("$", 1)[0]
    return hmac.compare_digest(hash_pw(pw, salt), stored)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")


def ip_tag(ip: str) -> str:
    parts = ip.split(".")
    return ".".join(parts[:2]) if len(parts) == 4 else ""


_last_comment: dict[str, float] = {}

COUNTS = (
    "(SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS n_comments,"
    "(SELECT COUNT(*) FROM reactions r WHERE r.post_id = p.id) AS n_reactions"
)


@app.get("/")
def board(request: Request, page: int = 1, kind: str = "", q: str = ""):
    page = max(page, 1)
    where, args = [], []
    if kind in KINDS:
        where.append("p.kind = ?")
        args.append(kind)
    elif kind == "hot":
        where.append("(SELECT COUNT(*) FROM reactions r WHERE r.post_id = p.id)"
                     " + (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) >= 10")
    if q.strip():
        where.append("p.title LIKE ?")
        args.append(f"%{q.strip()}%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM posts p {clause}", args).fetchone()[0]
        posts = conn.execute(
            f"SELECT p.*, {COUNTS} FROM posts p {clause} ORDER BY p.published_at DESC LIMIT ? OFFSET ?",
            [*args, PAGE_SIZE, (page - 1) * PAGE_SIZE],
        ).fetchall()
        last = conn.execute("SELECT MAX(collected_at) FROM posts").fetchone()[0]
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    first = max(1, min(page - 4, pages - 9))
    return templates.TemplateResponse(request, "board.html", {
        "posts": posts, "page": page, "pages": pages, "page_range": range(first, min(first + 9, pages) + 1),
        "kind": kind, "q": q, "total": total, "last": last, "u": ServerUrls(q),
    })


@app.get("/privacy")
def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {"u": ServerUrls()})


@app.get("/post/{post_id}")
def post_view(request: Request, post_id: int, err: str = ""):
    with db() as conn:
        conn.execute("UPDATE posts SET views = views + 1 WHERE id = ?", (post_id,))
        post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if not post:
            raise HTTPException(404, "없는 글입니다")
        reactions = conn.execute(
            "SELECT * FROM reactions WHERE post_id = ? ORDER BY created_at", (post_id,)).fetchall()
        comments = conn.execute(
            "SELECT * FROM comments WHERE post_id = ? ORDER BY created_at", (post_id,)).fetchall()
    return templates.TemplateResponse(request, "post.html", {
        "post": post, "reactions": reactions, "comments": comments, "err": err, "u": ServerUrls(),
    })


def back(post_id: int, err: str = "") -> RedirectResponse:
    url = f"/post/{post_id}" + (f"?err={err}" if err else "") + "#comments"
    return RedirectResponse(url, status_code=303)


@app.post("/post/{post_id}/comments")
def comment_add(request: Request, post_id: int,
                nickname: str = Form("ㅇㅇ"), password: str = Form(""), body: str = Form("")):
    nickname = nickname.strip()[:20] or "ㅇㅇ"
    body = body.strip()
    if not body or len(body) > 1000:
        return back(post_id, "body")
    if len(password) < 2:
        return back(post_id, "pw")
    ip = client_ip(request)
    now = time.time()
    if now - _last_comment.get(ip, 0) < COMMENT_COOLDOWN_SEC:
        return back(post_id, "fast")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone():
            raise HTTPException(404, "없는 글입니다")
        conn.execute(
            "INSERT INTO comments(post_id, nickname, pw_hash, ip_tag, body, created_at) VALUES (?,?,?,?,?,?)",
            (post_id, nickname, hash_pw(password), ip_tag(ip), body, int(now)),
        )
    _last_comment[ip] = now
    return back(post_id)


@app.post("/comments/{comment_id}/delete")
def comment_delete(comment_id: int, password: str = Form("")):
    with db() as conn:
        row = conn.execute("SELECT post_id, pw_hash FROM comments WHERE id = ?", (comment_id,)).fetchone()
        if not row:
            raise HTTPException(404, "없는 댓글입니다")
        if not check_pw(password, row["pw_hash"]):
            return back(row["post_id"], "del")
        conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    return back(row["post_id"])
