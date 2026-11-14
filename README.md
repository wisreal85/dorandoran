# 도란도란

AI 생태계 소식을 퍼 나르는 게시판. 무료로 열려 있는 통로(RSS, 공개 API)에서 글과 바깥 반응을 주기적으로 긁어온다.

- `app/collector.py` — 수집기 (`python -m app.collector`)
- `app/build.py` — 정적 사이트 빌드 (`python -m app.build`, 결과는 `_site/`)
- `app/main.py` — 익명 댓글이 되는 서버 버전 (`uvicorn app.main:app`)
- `.github/workflows/collect.yml` — 30분마다 수집해 GitHub Pages에 배포
