"""DB 내용을 정적 HTML로 굽는다 (GitHub Pages용).

실행:  python -m app.build        → _site/ 에 생성
"""
import shutil
import sys
from pathlib import Path

from .db import db, init
from .view import ADSENSE, BASE, HOT_MIN, TABS, make_templates

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE.parent / "_site"
PAGE_SIZE = 40
MAX_PAGES = 15

COUNTS = (
    "(SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS n_comments,"
    "(SELECT COUNT(*) FROM reactions r WHERE r.post_id = p.id) AS n_reactions"
)


class StaticUrls:
    """정적 사이트의 주소 규칙. 하위 경로에 올라가도 되도록 전부 상대 경로."""
    static = True

    def __init__(self, root: str):
        self.root = root
        self.css = root + "static/style.css"
        self.privacy = root + "privacy.html"

    def board(self, kind: str = "", page: int = 1) -> str:
        return f"{self.root}{kind or 'index'}{'' if page == 1 else f'-{page}'}.html"

    def post(self, post_id: int) -> str:
        return f"{self.root}p/{post_id}.html"


def main() -> None:
    init()
    env = make_templates().env
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "p").mkdir(parents=True)
    shutil.copytree(BASE / "static", OUT / "static")
    (OUT / ".nojekyll").write_text("")

    with db() as conn:
        posts = conn.execute(f"SELECT p.*, {COUNTS} FROM posts p ORDER BY p.published_at DESC").fetchall()
        last = conn.execute("SELECT MAX(collected_at) FROM posts").fetchone()[0]
        reactions, comments = {}, {}
        for r in conn.execute("SELECT * FROM reactions ORDER BY created_at"):
            reactions.setdefault(r["post_id"], []).append(r)
        for c in conn.execute("SELECT * FROM comments ORDER BY created_at"):
            comments.setdefault(c["post_id"], []).append(c)

    board, u = env.get_template("board.html"), StaticUrls("./")
    for kind, _ in TABS:
        if kind == "hot":
            rows = [p for p in posts if p["n_reactions"] + p["n_comments"] >= HOT_MIN]
        else:
            rows = [p for p in posts if not kind or p["kind"] == kind]
        pages = max(min((len(rows) + PAGE_SIZE - 1) // PAGE_SIZE, MAX_PAGES), 1)
        for page in range(1, pages + 1):
            first = max(1, min(page - 4, pages - 9))
            html = board.render(
                posts=rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE], page=page, pages=pages,
                page_range=range(first, min(first + 9, pages) + 1), kind=kind, q="", total=len(rows), last=last, u=u,
            )
            (OUT / u.board(kind, page)).write_text(html, encoding="utf-8")

    post_tpl, u = env.get_template("post.html"), StaticUrls("../")
    for p in posts:
        html = post_tpl.render(post=p, reactions=reactions.get(p["id"], []), comments=comments.get(p["id"], []), err="", u=u)
        (OUT / "p" / f"{p['id']}.html").write_text(html, encoding="utf-8")

    (OUT / "privacy.html").write_text(env.get_template("privacy.html").render(u=StaticUrls("./")), encoding="utf-8")
    if ADSENSE:  # 하위 경로용 사본. 애드센스가 실제로 읽는 것은 도메인 루트의 ads.txt
        (OUT / "ads.txt").write_text(f"google.com, {ADSENSE.replace('ca-', '')}, DIRECT, f08c47fec0942fa0\n")
    print(f"{len(posts)}개 글을 {OUT} 에 구웠습니다")


if __name__ == "__main__":
    main()
