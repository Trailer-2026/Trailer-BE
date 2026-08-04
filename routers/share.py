# -*- coding: utf-8 -*-
"""릴스 공유 페이지 — 카톡·SNS 에 붙일 링크가 열리는 곳.

버킷 mp4 주소를 그대로 공유하면 미리보기 없이 파일이 떨어지고, 삭제된 릴스도 링크만
있으면 영원히 재생된다. 이 페이지는 DB 를 거치므로 삭제·렌더중 릴스를 404 로 끊는다.

mp4 주소는 페이지 소스에 그대로 남는다 — 여긴 보안 경계가 아니라 공유 품질(미리보기·
링크 폐기) 장치다. 버킷 자체를 못 긁게 막는 건 IAM(allUsers 에서 objects.list 제거) 몫이다.
API 가 아니라 브라우저용 HTML 이라 include_in_schema=False (노션 스펙 동기화 대상 아님).
"""
import html

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from databases.database import get_db
from services import video_service

router = APIRouter(tags=["Share"], include_in_schema=False)

_DEFAULT_TITLE = "TRAILER 여행 릴스"

_STYLE = (
    "html,body{margin:0;height:100%;background:#000;color:#fff;"
    "font-family:system-ui,-apple-system,sans-serif}"
    ".wrap{height:100%;display:flex;flex-direction:column;align-items:center;"
    "justify-content:center;gap:14px;padding:16px;box-sizing:border-box}"
    "video{max-width:100%;max-height:80vh;border-radius:12px}"
    "p{margin:0;font-size:15px;opacity:.85;text-align:center}"
)


def _page(title: str | None, video_url: str) -> str:
    """릴스 재생 페이지 HTML. 제목은 사용자 입력이라 반드시 이스케이프한다."""
    safe_title = html.escape(title or _DEFAULT_TITLE)
    safe_url = html.escape(video_url)
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{safe_title}</title>"
        '<meta property="og:type" content="video.other">'
        f'<meta property="og:title" content="{safe_title}">'
        f'<meta property="og:video" content="{safe_url}">'
        f'<meta property="og:video:secure_url" content="{safe_url}">'
        '<meta property="og:video:type" content="video/mp4">'
        f"<style>{_STYLE}</style></head><body><div class=\"wrap\">"
        f'<video src="{safe_url}" controls autoplay muted playsinline></video>'
        f"<p>{safe_title}</p></div></body></html>"
    )


def _not_found_page() -> str:
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>영상을 찾을 수 없습니다</title>"
        f"<style>{_STYLE}</style></head><body><div class=\"wrap\">"
        "<p>영상을 찾을 수 없습니다.<br>삭제되었거나 아직 준비 중인 릴스입니다.</p>"
        "</div></body></html>"
    )


@router.get("/r/{reels_idx}", response_class=HTMLResponse)
def reels_share_page(reels_idx: int, db: Session = Depends(get_db)) -> HTMLResponse:
    reels = video_service.get_shared_reels(db, reels_idx)
    if reels is None:
        return HTMLResponse(_not_found_page(), status_code=404)
    return HTMLResponse(_page(reels.title, reels.url))


def _selfcheck() -> None:
    """제목 이스케이프 셀프체크(DB·네트워크 없음) — 실행: python -m routers.share."""
    page = _page('<script>alert(1)</script>', "https://x/v.mp4")
    assert "<script>alert" not in page, page
    assert "&lt;script&gt;" in page
    assert _DEFAULT_TITLE in _page(None, "https://x/v.mp4")
    print("OK: 공유 페이지 셀프체크 통과")


if __name__ == "__main__":
    _selfcheck()
