# -*- coding: utf-8 -*-
"""여행 경로 3D 영상(videoMaker) 서비스.

사진 EXIF·여행 일정(schedule)에서 뽑은 GPS 지점/사진/옵션을 travel_data.json 으로 변환해
services/videoMaker/modal_call.py 를 서브프로세스로 실행하고, 완성된 mp4 파일명을
돌려준다. 여행 렌더는 travel/schedule/travel_image 를 읽고, 렌더 완료 시 reels 행을
등록한다.

렌더는 **Modal T4 GPU 전용**이다 (서버 GPU 로 직접 돌리던 local 엔진은 제거됨).
modal_call.py 가 배포된 Modal 함수를 조각별로 원격 호출하고, 돌아온 조각들을
서버에서 합쳐(ffmpeg) 최종 mp4 를 만든다. 사전 1회 `modal deploy modal_render.py` 필요.

modal_call.py 를 실행할 파이썬은 properties_dev.ini 의 [videomaker] python 으로
지정한다(없으면 현재 프로세스 파이썬). 이 파이썬 환경에는 modal 과 playwright 가
설치돼 있어야 한다 — modal_call.py 가 render_video.py 를 임포트하는데 그 모듈이
최상위에서 playwright 를 임포트하기 때문이다(Chromium 브라우저 바이너리는 불필요).
"""
from __future__ import annotations

import io
import json
import logging
import math
import mimetypes
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

import requests
from PIL import Image
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import Config
from core.exceptions.custom import (
    BadRequestException,
    ExternalServiceException,
    NotFoundException,
)
from databases.daos import (
    ban_dao,
    comment_dao,
    like_dao,
    reels_dao,
    schedule_dao,
    travel_dao,
    travel_image_dao,
)
from schemas.video_schema import (
    MyReelsItem,
    MyReelsListResponse,
    ReelsRecommendResponse,
    ReelsShareResponse,
    ReelsUrlResponse,
    ReelsTitleUpdateResponse,
    ReelsUploadResponse,
    PromoRenderRequest,
)
from utils import cover_image, gcs, kakao_local, tour_place
from utils.timezone import KST

logger = logging.getLogger(__name__)


VIDEO_MAKER_DIR = Path(__file__).resolve().parent / "videoMaker"
BGM_DIR = VIDEO_MAKER_DIR / "bgm"
UPLOADS_DIR = VIDEO_MAKER_DIR / "assets" / "uploads"
OUTPUT_DIR = VIDEO_MAKER_DIR / "output"
MAP_THEMES_JS = VIDEO_MAKER_DIR / "map_themes.js"
# 배포된 Modal 함수를 호출하는 러너 (사전 1회: modal deploy modal_render.py)
MODAL_CALL_SCRIPT = VIDEO_MAKER_DIR / "modal_call.py"
# 로컬 GPU 렌더러 — VIDEO_ENGINE=local 일 때만 쓴다 (_engine 참고).
RENDER_SCRIPT = VIDEO_MAKER_DIR / "render_video.py"

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
RENDER_TIMEOUT_SECONDS = 60 * 30  # 30분 하드 캡 (슬롯을 잡은 뒤부터 센다 — 대기는 안 센다)
# 동시에 돌릴 렌더 편수. 편당 Modal GPU 컨테이너를 modal_call.MAX_CHUNKS(2)개 쓰고,
# 조각 합치기·BGM·인트로/아웃트로는 이 서버의 ffmpeg 가 한다. 무제한으로 띄우면 Modal
# 동시 GPU 한도를 넘겨 렌더가 **실패로** 떨어지는데, 실패하면 자리표 릴스 행이 지워져
# 사용자에겐 이유 없이 사라진 것처럼 보인다. 그래서 초과 요청은 거절하지 않고 슬롯이
# 빌 때까지 기다린다(앱은 이미 진행률을 폴링하므로 phase 만 "대기 중"으로 바뀐다).
# Modal 플랜을 올렸으면 환경변수로 키운다 — **N × 2 ≤ 계정 동시 GPU 한도**.
# 값이 이상해도 부팅을 막지 않고 기본값으로 떨어진다. 판정은 isdigit() 이 아니라
# int() 로 해야 한다 — "²" 같은 문자는 isdigit() 이 True 인데 int() 는 ValueError 다.
try:
    _concurrency = int(os.getenv("RENDER_CONCURRENCY", ""))
except ValueError:
    _concurrency = 0
RENDER_CONCURRENCY = _concurrency if _concurrency > 0 else 3
# ponytail: 프로세스 안에서만 세는 슬롯이다. 다중 워커로 띄우면 워커마다 이 수만큼
# 돌아 한도를 넘는다 — 그 땐 워커 수로 나눠 잡거나 큐를 밖으로 빼야 한다.
_render_slots = threading.BoundedSemaphore(RENDER_CONCURRENCY)
# render_video.py --theme 와 map_themes.js THEMES 에 맞춰 유지.
ALLOWED_THEMES = {"default", "spring", "summer", "autumn", "winter"}


# --------------------------------------------------------------------------- #
# 정적 자원
# --------------------------------------------------------------------------- #
def get_map_themes_path() -> Path:
    """빌더 미리보기가 렌더러와 공유하는 테마 모듈 경로."""
    if not MAP_THEMES_JS.is_file():
        raise NotFoundException("map_themes.js가 없습니다.")
    return MAP_THEMES_JS


def get_output_path(name: str) -> Path:
    """완성 영상 경로를 output/ 밖으로 못 나가게 검증해 반환한다.

    릴스 url 이 GCS 가 아닌 옛 데이터를 다운로드할 때만 쓰인다 (파일명으로 직접
    받아가던 /api/videos/output/{name} 엔드포인트는 없앴다).
    """
    candidate = (OUTPUT_DIR / name).resolve()
    if candidate.parent != OUTPUT_DIR.resolve() or not candidate.is_file():
        raise NotFoundException("영상을 찾을 수 없습니다.")
    return candidate


# 버킷 영상을 클라이언트로 흘려보낼 때의 조각 크기 (메모리에 통째로 안 올리려고).
DOWNLOAD_CHUNK_BYTES = 1024 * 1024


def _load_own_reels(db: Session, reels_idx: int, user_idx: int):
    """본인 릴스를 조회한다 (진행률·다운로드·편집 공용). 없거나 남의 릴스면 404.

    남의 릴스는 403 이 아니라 404 로 답한다 — 존재 여부 자체를 알리지 않는다.
    렌더가 아직 안 끝난 행도 그대로 돌려준다(진행률 조회가 그 행을 봐야 한다).
    """
    reels = reels_dao.get_by_idx(db, reels_idx)
    if reels is None or reels.user_idx != user_idx:
        raise NotFoundException("릴스를 찾을 수 없습니다.")
    return reels


def _load_ready_reels(db: Session, reels_idx: int, user_idx: int):
    """영상이 완성된 본인 릴스를 조회한다 (다운로드·편집용). 렌더 중이면 400."""
    reels = _load_own_reels(db, reels_idx, user_idx)
    if not reels.url:
        raise BadRequestException(
            "아직 렌더링이 끝나지 않은 릴스입니다. "
            "GET /api/videos/render/{reels_idx} 로 진행률을 확인하세요."
        )
    return reels


def get_reels_download(
    db: Session, reels_idx: int, user_idx: int
) -> tuple[object, str, int | None]:
    """본인 릴스 영상 다운로드 소스를 (스트림|로컬 경로, 파일명, 크기)로 돌려준다.

    영상은 보통 GCS 버킷에 있으므로 버킷 객체를 열어 조각 단위로 흘려보낸다
    (서버 디스크에 받아두지 않는다). 버킷 업로드에 실패해 로컬에만 남은 옛 영상은
    output/ 파일 경로를 돌려주고 크기는 None 이다. 본인 릴스가 아니거나 영상이
    없으면 404, 아직 렌더 중이면 400.
    """
    reels = _load_ready_reels(db, reels_idx, user_idx)

    filename = f"reels_{reels_idx}.mp4"
    url = reels.url or ""
    object_path = gcs.object_path_from_url(url)
    if object_path is None:
        # 우리 버킷 URL 이 아님 → 렌더 당시 업로드 실패로 로컬에만 남은 경우.
        return get_output_path(Path(url).name), filename, None
    if not gcs.object_exists(object_path):
        raise NotFoundException("영상을 찾을 수 없습니다.")

    stream, size = gcs.open_object(object_path)

    def chunks():
        try:
            while chunk := stream.read(DOWNLOAD_CHUNK_BYTES):
                yield chunk
        finally:
            stream.close()

    return chunks(), filename, size


# --------------------------------------------------------------------------- #
# BGM
# --------------------------------------------------------------------------- #
def _bgm_display_name(filename: str) -> dict[str, str]:
    """`곡명 - 아티스트.mp3` 형식 파일명을 곡명/아티스트로 정리한다 (bgm/CREDITS.md 참고)."""
    stem = Path(filename).stem
    if " - " in stem:
        title, artist = stem.rsplit(" - ", 1)
        return {"title": title.strip(), "artist": artist.strip(), "source": "Pixabay"}
    return {"title": stem, "artist": "", "source": ""}


def _bgm_tracks() -> list[Path]:
    """bgm/ 폴더의 오디오 트랙 경로 목록 (정렬 순서 고정)."""
    if not BGM_DIR.exists():
        return []
    return [
        path
        for path in sorted(BGM_DIR.iterdir())
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
    ]


def list_bgm() -> list[dict[str, str]]:
    """bgm/ 폴더의 트랙 목록. file 값을 렌더 요청의 bgm 필드에 그대로 쓴다."""
    return [{"file": path.name, **_bgm_display_name(path.name)} for path in _bgm_tracks()]


def get_bgm_path(filename: str) -> Path:
    """BGM 이름을 bgm/ 트랙 경로로 해석한다 (bgm/ 밖으로 경로 탈출 불가).

    파일명 완전 일치 외에 대소문자 무시·확장자 생략·곡명("Funk")만 보내는
    형태도 허용한다 — 파일이 개명되거나 프론트가 표시명으로 보내도 매칭되게.
    """
    query = (filename or "").strip()
    candidate = (BGM_DIR / query).resolve()
    if candidate.parent == BGM_DIR.resolve() and candidate.is_file():
        if candidate.suffix.lower() not in AUDIO_EXTENSIONS:
            raise BadRequestException("오디오 파일이 아닙니다.")
        return candidate

    # 완전 일치 실패 → 확장자 떼고 대소문자 무시로 파일명 → 곡명 → 유일한
    # 부분 일치 순서로 찾는다. 후보는 bgm/ 안의 오디오 파일뿐이라 탈출 위험 없음.
    tracks = _bgm_tracks()
    normalized = Path(query).stem.strip().casefold()
    if normalized:
        for track in tracks:
            if track.stem.casefold() == normalized:
                return track
        for track in tracks:
            if track.stem.rsplit(" - ", 1)[0].strip().casefold() == normalized:
                return track
        partial = [t for t in tracks if normalized in t.stem.casefold()]
        if len(partial) == 1:
            return partial[0]
    available = ", ".join(t.name for t in tracks) or "(bgm 폴더가 비어 있음)"
    raise NotFoundException(f"BGM을 찾을 수 없습니다: {query} (사용 가능: {available})")


# --------------------------------------------------------------------------- #
# 릴스 추천
# --------------------------------------------------------------------------- #
RECOMMEND_REELS_COUNT = 10


def recommend_reels(
    db: Session, exclude: str, user=None, count: int = RECOMMEND_REELS_COUNT
) -> list[ReelsRecommendResponse]:
    """릴스를 무작위로 최대 count(기본 10)개 추천한다.

    홈 화면의 "지금 사람들이 떠나는 여행" 카드는 같은 목록을 count 만 줄여 쓴다 —
    카드에 필요한 지역 태그(region)·썸네일(thumbnail_url)이 여기 같이 내려간다.

    exclude(쉼표 구분 reels_idx 목록)에 담긴 릴스는 제외하고 뽑는다 — 프론트가
    이미 받은 idx를 누적해 재요청하면 새 릴스만 내려간다. 남은 릴스가 10개
    미만이면 있는 만큼만 반환하고, 제외 후 남은 릴스가 하나도 없으면 exclude를
    무시하고 전체에서 처음부터 다시 추천한다.
    로그인 상태면 내가 차단한 사용자의 릴스와 **내가 올린 릴스**가 두 경로 모두에서
    빠진다(비로그인은 user=None 이라 전체에서 뽑는다 — 익명 호출자에겐 '내 것'이 없다).
    """
    exclude_idxs: list[int] = []
    for token in exclude.split(","):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit():
            raise BadRequestException("exclude는 쉼표로 구분한 reels_idx 목록이어야 합니다.")
        exclude_idxs.append(int(token))

    # 차단 목록은 두 군데에 쓰이는데 범위가 다르다. **나 자신은 차단이 아니다** —
    # 릴스 노출에서만 빼고(아래 hidden_users), 댓글 수에서 빼면 내가 쓴 댓글이 안 세어진다.
    blocked = ban_dao.blocked_user_idxs(db, user.user_idx) if user else []
    # 차단한 사용자 + 나 자신 — 내 릴스는 마이페이지에서 보면 되고, 남의 여행을
    # 구경하는 피드에 내가 올린 게 섞이면 그만큼 볼 게 줄어든다.
    hidden_users = [*blocked, user.user_idx] if user else []
    rows = reels_dao.get_random_reels(db, count, exclude_idxs, hidden_users)
    if not rows and exclude_idxs:
        # 전부 이미 추천된 상태 → 한 바퀴 돌았으니 처음부터 다시
        rows = reels_dao.get_random_reels(db, count, [], hidden_users)
    # 좋아요·댓글 수는 행마다 세면 N+1 이라 뽑힌 릴스만 묶어 두 번에 읽는다.
    idxs = [reels.reels_idx for reels, _, _ in rows]
    like_counts = like_dao.counts_by_reels(db, idxs)
    # 댓글 수는 '내가 볼 수 있는 수' — 차단한 사람의 댓글은 목록에서 빠지므로 숫자에서도 뺀다.
    comment_counts = comment_dao.counts_by_reels(db, idxs, blocked)
    liked = like_dao.liked_reels_idxs(db, user.user_idx, idxs) if user else set()
    return [
        ReelsRecommendResponse(
            reels_idx=reels.reels_idx,
            url=reels.url,
            title=reels.title,
            region=reels.region,
            thumbnail_url=reels.thumbnail_url,
            like_count=like_counts.get(reels.reels_idx, 0),
            comment_count=comment_counts.get(reels.reels_idx, 0),
            is_liked=reels.reels_idx in liked,
            user_idx=reels.user_idx,
            nickname=nickname,
            profile_image=profile_image,
        )
        for reels, nickname, profile_image in rows
    ]


# --------------------------------------------------------------------------- #
# 마이페이지 릴스 (내가 올린 / 좋아요한)
# --------------------------------------------------------------------------- #
def list_my_reels(
    db: Session, user, limit: int, cursor: int | None
) -> MyReelsListResponse:
    """마이페이지 "내가 올린 릴스" — 최신순, 커서 페이징.

    작성자가 호출자뿐이라 닉네임·프로필은 조인 없이 current_user 에서 채운다.
    내 릴스만 나오니 차단으로 걸러질 릴스는 없지만, **댓글 수는 차단을 타므로**
    (내 릴스에 차단한 사람이 단 댓글은 목록에서 빠진다) 차단 목록은 조회한다.
    """
    rows = reels_dao.list_by_user(db, user.user_idx, limit + 1, cursor)
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = _to_reels_cards(
        db, user, [(reels, user.nickname, user.profile_image) for reels in rows],
        ban_dao.blocked_user_idxs(db, user.user_idx),
    )
    return MyReelsListResponse(
        items=items,
        next_cursor=rows[-1].reels_idx if has_more and rows else None,
    )


def list_liked_reels(
    db: Session, user, limit: int, cursor: int | None
) -> MyReelsListResponse:
    """마이페이지 "좋아요한 릴스" — 내가 좋아요를 누른 순, 커서 페이징.

    별도의 북마크 테이블은 없다. 앱의 하트가 곧 저장이라 likes 테이블(reels_idx 가
    채워진 행)을 그대로 목록으로 읽는다.

    내가 차단한 사용자의 릴스는 뺀다 — 추천 피드(recommend_reels)와 같은 규칙이라,
    차단 후에도 예전에 누른 하트 때문에 그 사람 릴스가 여기 남아 있으면 안 된다.
    커서는 likes_idx 다(reels_idx 가 아니다 — reels_dao.list_liked_by_user 참고).
    """
    blocked = ban_dao.blocked_user_idxs(db, user.user_idx)
    rows = reels_dao.list_liked_by_user(db, user.user_idx, limit + 1, cursor, blocked)
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = _to_reels_cards(
        db, user,
        [(reels, nickname, profile_image) for _, reels, nickname, profile_image in rows],
        blocked,  # 위에서 이미 조회했다 — 카드 댓글 수에도 같은 목록을 쓴다
    )
    return MyReelsListResponse(
        items=items,
        next_cursor=rows[-1][0] if has_more and rows else None,
    )


def _to_reels_cards(db: Session, user, rows, blocked: list[int]) -> list[MyReelsItem]:
    """(릴스, 닉네임, 프로필) 목록에 좋아요·댓글 수와 내 좋아요 여부를 붙인다.

    recommend_reels 와 같은 이유로 카운트는 행마다 세지 않고 뽑힌 릴스만 묶어
    일괄 조회한다(N+1 회피).
    blocked(내가 차단한 사용자)는 댓글 수에서 뺀다 — 댓글 목록에서도 빠지므로
    숫자와 목록이 어긋나지 않게 한다(comment_dao.counts_by_reels 참조).
    """
    idxs = [reels.reels_idx for reels, _, _ in rows]
    like_counts = like_dao.counts_by_reels(db, idxs)
    comment_counts = comment_dao.counts_by_reels(db, idxs, blocked)
    liked = like_dao.liked_reels_idxs(db, user.user_idx, idxs)
    return [
        MyReelsItem(
            reels_idx=reels.reels_idx,
            url=reels.url,
            title=reels.title,
            region=reels.region,
            thumbnail_url=reels.thumbnail_url,
            like_count=like_counts.get(reels.reels_idx, 0),
            comment_count=comment_counts.get(reels.reels_idx, 0),
            is_liked=reels.reels_idx in liked,
            user_idx=reels.user_idx,
            nickname=nickname,
            profile_image=profile_image,
        )
        for reels, nickname, profile_image in rows
    ]


# --------------------------------------------------------------------------- #
# 릴스 제목 수정
# --------------------------------------------------------------------------- #
def update_reels_title(
    db: Session, reels_idx: int, user_idx: int, title: str | None
) -> ReelsTitleUpdateResponse:
    """본인 릴스의 제목만 바꾼다. 남의 릴스·없는 릴스는 404, 렌더 중이면 400.

    제목 정규화는 렌더 시작 때와 같은 _clean_title 을 쓴다 — 공백만 보내면 제목
    없음(None)이 되어, 처음부터 title 없이 렌더한 릴스와 같은 상태가 된다.
    영상을 건드리지 않으므로 url·썸네일·PK 는 그대로다.
    """
    reels = _load_ready_reels(db, reels_idx, user_idx)
    reels_dao.update_title(db, reels, _clean_title(title))
    db.commit()
    return ReelsTitleUpdateResponse(reels_idx=reels.reels_idx, title=reels.title)


def delete_reels(db: Session, reels_idx: int, user_idx: int) -> None:
    """본인 릴스를 삭제한다. 없거나 남의 릴스면 404.

    행은 **소프트 삭제**다 — 댓글·좋아요가 FK 로 참조하고 있어 하드 삭제하면 무결성이
    깨진다(렌더 실패 자리표만 하드 삭제하는 것과 다르다). 소프트 삭제만으로 추천
    피드·마이페이지·공유 링크에서 즉시 사라진다(읽기 쿼리가 deleted_at 을 거른다).

    커밋 뒤 영상·썸네일 객체도 버킷에서 지운다 — 공개 버킷이라 URL 을 아는 사람은
    행이 지워져도 계속 볼 수 있기 때문이다. 정리 실패는 삼키되 객체 경로를 경고
    로그로 남긴다(고아 객체가 남는 게 이미 커밋된 삭제를 502 로 되돌리는 것보다
    낫다 — 대신 로그의 경로로 나중에 손으로 지울 수 있다). 그래서 엔드포인트 설명도
    '지워진다'가 아니라 '삭제를 시도한다'로 적혀 있다.

    렌더가 아직 안 끝난 릴스도 지울 수 있다 — 멈춘 렌더를 치우는 게 사용자가 원하는
    동작이다. 그 뒤 렌더가 끝나면 _publish_reels_video 가 삭제된 행을 알아채고(삭제
    안 된 행에만 거는 조건부 갱신) 방금 올린 영상·썸네일을 버킷에서 되돌린다.
    """
    reels = _load_own_reels(db, reels_idx, user_idx)
    video_url, thumbnail_url = reels.url, reels.thumbnail_url
    reels_dao.soft_delete(db, reels)
    db.commit()
    _delete_object_quietly(video_url, "삭제한 릴스 영상")
    _delete_object_quietly(thumbnail_url, "삭제한 릴스 썸네일")


# --------------------------------------------------------------------------- #
# 릴스 공유
# --------------------------------------------------------------------------- #
def get_shared_reels(db: Session, reels_idx: int):
    """공유 페이지에 띄울 릴스 — 없거나 삭제됐거나 렌더 중이면 None.

    릴스는 공개 피드라 소유자를 따지지 않는다(추천 API 도 남의 릴스 url 을 그대로
    내려준다). DB 를 거치는 덕에 삭제된 릴스는 공유 링크가 즉시 죽는다 — 버킷 URL 을
    직접 공유했을 때는 못 하던 일이다.
    """
    reels = reels_dao.get_by_idx(db, reels_idx)
    return reels if reels is not None and reels.url else None


def get_reels_url(db: Session, reels_idx: int, user_idx: int) -> ReelsUrlResponse:
    """reels_idx 로 릴스 영상 주소를 되찾는다. 없거나 삭제됐거나 렌더 중이면 404.

    앱 딥링크(특히 편집 화면)가 영상 URL 을 파라미터로 들고 다니면 외부에서 만든
    링크로 남의(또는 우리 것이 아닌) 영상을 앱 화면에 띄울 수 있다. 그래서 딥링크는
    reels_idx 만 싣고 실제 주소는 이 API 로 받아간다 — 서버가 준 PK 에 대응하는
    주소만 나가므로 임의 URL 이 끼어들 자리가 없다.

    소유자를 따지지 않는 건 릴스가 공개 피드라서다(공유 페이지·추천 API 와 같다).
    대신 `is_mine` 을 함께 내려 편집 화면을 열지 말지 앱이 먼저 판단할 수 있게 한다
    (편집 API 자체도 남의 릴스면 404 라 서버 쪽 방어는 그대로다).
    """
    reels = get_shared_reels(db, reels_idx)
    if reels is None:
        raise NotFoundException("릴스를 찾을 수 없습니다.")
    return ReelsUrlResponse(
        reels_idx=reels.reels_idx,
        url=reels.url,
        title=reels.title,
        is_mine=reels.user_idx == user_idx,
    )


def get_share_link(db: Session, reels_idx: int, request_base_url: str) -> ReelsShareResponse:
    """릴스 공유 링크(/r/{reels_idx}) 를 만들어 준다. 공유 불가 릴스면 404.

    도메인은 [app] share_base_url 이 있으면 그 값, 없으면 요청 자체의 호스트를 쓴다.
    리버스 프록시 뒤에서 내부 호스트가 잡히면 그 설정으로 덮어라.
    """
    reels = get_shared_reels(db, reels_idx)
    if reels is None:
        raise NotFoundException("릴스를 찾을 수 없습니다.")
    base = (Config.read("app", "share_base_url", "") or request_base_url).rstrip("/")
    return ReelsShareResponse(share_url=f"{base}/r/{reels.reels_idx}", title=reels.title)


# --------------------------------------------------------------------------- #
# 렌더링
# --------------------------------------------------------------------------- #
def _render_python() -> str:
    """modal_call.py 를 실행할 파이썬 경로 (modal·playwright 가 설치된 환경)."""
    return Config.read("videomaker", "python", default=sys.executable) or sys.executable


def _engine() -> str:
    """렌더 엔진 — 기본 "modal", VIDEO_ENGINE=local 이면 이 서버의 GPU 로 돌린다.

    로컬은 **개발 기기에서 눈으로 확인하려고** 두는 경로다: render_video.py 를 직접
    띄우면 --gpu-mode 기본값 auto(Windows 에서 --use-angle=d3d11)로 로컬 GPU 를 쓴다.
    폼·스키마를 안 건드리고 환경변수 하나로만 갈리므로, 서버엔 이 값을 넣지 않는
    한 배포 동작은 그대로 modal 이다.
    """
    return "local" if os.getenv("VIDEO_ENGINE", "").strip().lower() == "local" else "modal"


def _build_command(
    travel_data_path: Path,
    theme: str,
    max_video_seconds: float | None = None,
    trailer_intro: bool = True,
) -> list[str]:
    """렌더 명령을 만든다 (기본 Modal, VIDEO_ENGINE=local 이면 로컬 GPU).

    화질은 항상 quality-fast(JPEG q95, 풀해상도) — 무손실 PNG 대비 최종 mp4
    화질 차이가 사실상 없고 렌더가 크게 빠르다(modal_call.py 의 기본 --mode).

    max_video_seconds 를 주면 **조각을 나누지 않는다**(--max-chunks 1) — 길이 상한은
    render_video 가 조각 하나 안에서 걸어서, 2조각으로 나누면 상한이 조각마다 따로
    걸려 전체는 두 배가 된다. 정확한 길이가 중요한 홍보 영상만 이 값을 준다.
    로컬은 애초에 한 프로세스가 통째로 렌더하므로 조각 옵션 자체가 없다.
    """
    local = _engine() == "local"
    command = [
        _render_python(),
        str(RENDER_SCRIPT if local else MODAL_CALL_SCRIPT),
        "--travel-data",
        travel_data_path.relative_to(VIDEO_MAKER_DIR).as_posix(),
    ]
    if local:
        command += ["--quality-fast"]  # modal_call 은 이게 기본이라 로컬만 명시한다
    if theme != "default":
        command += ["--theme", theme]
    if max_video_seconds is not None:
        command += ["--max-video-seconds", str(max_video_seconds)]
        if not local:
            command += ["--max-chunks", "1"]
    # 아웃트로는 붙이지 않는다. TRAILER 인트로는 표지 인트로가 없을 때만 붙인다
    # (표지가 있으면 렌더가 끝난 뒤 이 서버가 _prepend_cover_intro 로 대신 붙인다).
    if trailer_intro:
        command += ["--intro"]
    return command


def _parse_output_name(stdout: str) -> str | None:
    """렌더 서브프로세스 stdout 에서 완성 파일명을 뽑는다.

    modal_call.py 가 조각을 합친 뒤 "저장 위치: <path>" 를 출력한다. 로컬 엔진
    (render_video.py 직접 실행)은 그 줄이 없고 "출력 예정 파일: <path>" 만 찍는다 —
    Modal 경로에서는 그게 컨테이너 안 /app 경로라 쓸 수 없어 "저장 위치" 를 먼저 본다.
    """
    match = re.search(r"저장 위치:\s*(.+)", stdout)
    if match is None and _engine() == "local":
        match = re.search(r"출력 예정 파일:\s*(.+)", stdout)
    if match:
        return Path(match.group(1).strip()).name
    # 폴백: output/ 의 가장 최근 mp4.
    if OUTPUT_DIR.exists():
        mp4s = sorted(OUTPUT_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        if mp4s:
            return mp4s[-1].name
    return None


# --------------------------------------------------------------------------- #
# 완성 영상 편집 (ffmpeg 후처리) — 구간 삭제 / 이미지 삽입
#
# 편집 대상은 등록된 릴스(reels_idx)다. 완성 영상은 GCS 버킷에만 있고 서버에는
# 남지 않으므로(렌더 후 로컬 사본 삭제), 편집은 "릴스 URL 로 버킷에서 내려받기 →
# ffmpeg 처리 → 결과 재업로드 → reels.url 교체 → 이전 객체·임시파일 삭제" 로 돈다.
# 편집은 서버 CPU(ffmpeg)만 쓴다 — Modal GPU 는 렌더에만 필요하다.
# --------------------------------------------------------------------------- #
EDIT_TIMEOUT_SECONDS = 60 * 5
# 편집 결과물이 올라갈 버킷 경로 접두어 (렌더 원본 reels/ 와 구분).
EDIT_OBJECT_PREFIX = "reels/edited"
THUMBNAIL_OBJECT_PREFIX = "reels/thumb"
# 삽입 사진이 화면에 머무는 시간(초). 렌더러가 영상 안에서 사진 한 장을 보여주는
# 시간과 같은 값이라 삽입 클립만 튀지 않는다 — render_video.QUALITY_FAST_CONFIG 의
# photo_fade_in(0.4) + photo_hold(1.2) + photo_fade_out(0.4). 그쪽이 바뀌면 같이 고칠 것.
INSERT_PHOTO_SECONDS = 2.0
# 렌더러와 같은 계열의 인코딩 (정확한 컷을 위해 재인코딩 필수 — 스트림 카피는
# 키프레임 단위로만 잘려 구간이 밀린다).
_EDIT_VIDEO_ARGS = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
    "-pix_fmt", "yuv420p", "-movflags", "+faststart",
]


def _ffprobe_video(path: Path) -> dict[str, object]:
    """영상 메타데이터 조회: duration/has_audio/width/height/fps/sample_rate."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise ExternalServiceException("ffprobe를 찾을 수 없습니다 (PATH 확인).")
    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height,r_frame_rate,sample_rate",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if result.returncode != 0:
        raise ExternalServiceException(f"영상 정보를 읽지 못했습니다:\n{(result.stderr or '')[-500:]}")
    info = json.loads(result.stdout or "{}")
    streams = info.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {
        "duration": float(info.get("format", {}).get("duration", 0) or 0),
        "has_audio": audio_stream is not None,
        "width": int(video_stream.get("width", 0) or 0),
        "height": int(video_stream.get("height", 0) or 0),
        "fps": video_stream.get("r_frame_rate", "30/1") or "30/1",
        "sample_rate": int((audio_stream or {}).get("sample_rate", 44100) or 44100),
    }


def _edit_workspace() -> Path:
    """편집 임시 파일(원본 사본·결과물)을 담을 새 디렉터리. 끝나면 통째로 지운다."""
    work_dir = VIDEO_MAKER_DIR / "temp" / f"edit_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


def _download_source(video_url: str, work_dir: Path) -> Path:
    """편집할 원본 영상을 버킷에서 내려받는다. 우리 버킷 URL 이 아니면 400."""
    object_path = gcs.object_path_from_url((video_url or "").strip())
    if object_path is None:
        raise BadRequestException(
            "영상 저장소(GCS)에 없는 릴스라 편집할 수 없습니다 "
            "(버킷 업로드에 실패해 로컬에만 남은 영상)."
        )
    if not gcs.object_exists(object_path):
        raise NotFoundException("영상을 찾을 수 없습니다.")
    source = work_dir / "source.mp4"
    gcs.download_file(object_path, source)
    return source


def _run_ffmpeg(args: list[str]) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ExternalServiceException("ffmpeg를 찾을 수 없습니다 (PATH 확인).")
    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=EDIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise ExternalServiceException("영상 편집이 제한 시간(5분)을 초과했습니다.") from error
    if result.returncode != 0:
        raise ExternalServiceException(f"영상 편집 실패:\n{(result.stderr or '')[-1500:]}")


# 썸네일을 뽑는 시점(초). 영상 앞엔 TRAILER 인트로가 약 3초(intro_video 의
# HOLD+ZOOM) 붙으므로 그 뒤에서 떠야 로고 대신 실제 여행 장면이 잡힌다.
THUMBNAIL_AT_SECONDS = 3.5


def _publish_thumbnail(video_path: Path, at_seconds: float = THUMBNAIL_AT_SECONDS) -> str | None:
    """영상에서 대표 프레임 한 장을 뽑아 버킷에 올린다 → 공개 URL. 실패하면 None.

    홈 화면 카드가 영상을 받지 않고 그릴 수 있게 하는 부가 정보라, ffmpeg 이 없거나
    영상이 at_seconds 보다 짧아 프레임이 안 나와도 렌더/편집 자체는
    성공시켜야 한다 — 그래서 여기서 예외를 삼키고 경고만 남긴다.

    at_seconds 는 인트로가 붙는 렌더 결과 기준값이 기본이다. 사용자가 직접 올린
    영상엔 인트로가 없고 3.5초보다 짧을 수도 있어 업로드 경로가 값을 낮춰 준다.
    """
    target = video_path.with_name(f"{video_path.stem}_thumb.jpg")
    try:
        _run_ffmpeg([
            "-ss", f"{at_seconds:.1f}", "-i", str(video_path),
            "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "3", str(target),
        ])
        return gcs.upload_bytes(
            f"{THUMBNAIL_OBJECT_PREFIX}/{uuid.uuid4().hex}.jpg",
            target.read_bytes(), "image/jpeg",
        )
    except Exception:
        logger.warning("릴스 썸네일 생성 실패(무시): %s", video_path.name)
        return None
    finally:
        target.unlink(missing_ok=True)


def _delete_object_quietly(url: str | None, what: str) -> None:
    """버킷 객체를 지운다. 실패해도 로그만 남긴다(고아 객체는 서비스에 영향 없음)."""
    object_path = gcs.object_path_from_url(url or "")
    if not object_path:
        return
    try:
        gcs.delete_object(object_path)
    except Exception:
        logger.warning("%s 삭제 실패(무시): %s", what, object_path)


def _edit_result(db: Session, reels, started: float, target: Path) -> dict[str, object]:
    """편집 결과물을 버킷에 올리고 릴스 URL 을 교체한다 (target 은 임시 파일).

    reels.url 이 편집본을 가리키게 바꾸므로 릴스 PK 는 그대로고 영상만 갱신된다.
    교체에 성공하면 이전 객체는 아무도 참조하지 않으니 버킷에서 지운다(실패해도
    편집 자체는 성공 — 고아 객체 로그만 남긴다).
    """
    info = _ffprobe_video(target)
    # commit 이후엔 인스턴스 속성이 만료돼 재조회가 걸리므로 미리 읽어 둔다.
    reels_idx, previous_url = reels.reels_idx, reels.url
    previous_thumbnail = reels.thumbnail_url
    video_url = gcs.upload_file(
        f"{EDIT_OBJECT_PREFIX}/{uuid.uuid4().hex}.mp4", target, "video/mp4"
    )
    # 영상이 바뀌었으니 썸네일도 다시 뽑는다(앞부분을 잘라낸 편집이면 옛 썸네일이
    # 영상에 없는 장면이 된다). 실패하면 None 으로 비운다 — 편집 전 장면을 계속
    # 걸어두는 것보다 앱이 영상 프레임으로 폴백하는 편이 낫다.
    thumbnail_url = _publish_thumbnail(target)
    try:
        reels_dao.update_url(db, reels, video_url, thumbnail_url)
        db.commit()
    except Exception:
        db.rollback()
        gcs.delete_object(gcs.object_path_from_url(video_url) or video_url)  # 고아 객체 정리
        _delete_object_quietly(thumbnail_url, "고아 썸네일")
        raise

    # 새로 뽑았든 비웠든 릴스는 더 이상 옛 썸네일을 참조하지 않는다 → 항상 정리.
    _delete_object_quietly(previous_url, "편집 전 영상 객체")
    _delete_object_quietly(previous_thumbnail, "편집 전 썸네일")

    return {
        "reels_idx": reels_idx,
        "video_url": video_url,
        "duration_seconds": round(float(info["duration"]), 2),
        "elapsed_seconds": round(time.perf_counter() - started, 1),
    }


def cut_video(
    db: Session, reels_idx: int, user_idx: int, start_seconds: float, end_seconds: float
) -> dict[str, object]:
    """릴스 영상에서 [start, end) 구간을 잘라내고 릴스 URL 을 편집본으로 교체한다."""
    reels = _load_ready_reels(db, reels_idx, user_idx)
    work_dir = _edit_workspace()
    try:
        source = _download_source(reels.url, work_dir)
        info = _ffprobe_video(source)
        duration = float(info["duration"])
        start, end = float(start_seconds), float(end_seconds)
        if start < 0 or end <= start:
            raise BadRequestException("삭제 구간이 올바르지 않습니다 (0 ≤ 시작 < 끝).")
        if start >= duration:
            raise BadRequestException(f"시작 시각이 영상 길이({duration:.1f}초)를 넘습니다.")
        end = min(end, duration)

        # 남길 구간 목록: (시작, 끝|None=영상 끝까지). 경계에 붙은 삭제면 한 구간만 남는다.
        eps = 0.05
        keep: list[tuple[float, float | None]] = []
        if start > eps:
            keep.append((0.0, start))
        if end < duration - eps:
            keep.append((end, None))
        if not keep:
            raise BadRequestException("영상 전체를 삭제할 수는 없습니다.")

        # concat 필터 입력은 세그먼트 단위로 [v0][a0][v1][a1]... 처럼 끼워 넣어야 한다.
        filters: list[str] = []
        segment_labels: list[str] = []
        for i, (seg_start, seg_end) in enumerate(keep):
            rng = f"start={seg_start:.3f}" + (f":end={seg_end:.3f}" if seg_end is not None else "")
            filters.append(f"[0:v]trim={rng},setpts=PTS-STARTPTS[v{i}]")
            labels = f"[v{i}]"
            if info["has_audio"]:
                filters.append(f"[0:a]atrim={rng},asetpts=PTS-STARTPTS[a{i}]")
                labels += f"[a{i}]"
            segment_labels.append(labels)

        maps = ["-map", "[v]"]
        audio_args: list[str] = []
        if info["has_audio"]:
            filters.append(f"{''.join(segment_labels)}concat=n={len(keep)}:v=1:a=1[v][a]")
            maps += ["-map", "[a]"]
            audio_args = ["-c:a", "aac", "-b:a", "192k"]
        else:
            filters.append(f"{''.join(segment_labels)}concat=n={len(keep)}:v=1:a=0[v]")

        target = work_dir / "cut.mp4"
        started = time.perf_counter()
        _run_ffmpeg([
            "-i", str(source),
            "-filter_complex", ";".join(filters),
            *maps, *_EDIT_VIDEO_ARGS, *audio_args,
            str(target),
        ])
        return _edit_result(db, reels, started, target)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def insert_image(
    db: Session,
    reels_idx: int,
    user_idx: int,
    at_seconds: float,
    image_filename: str,
    image_bytes: bytes,
) -> dict[str, object]:
    """릴스 영상의 at 시점에 이미지를 전체 화면으로 끼워 넣고 URL 을 교체한다.

    본편 [0, at) 재생 → 사진 INSERT_PHOTO_SECONDS 초 → 본편 [at, 끝) 재생.
    사진이 머무는 시간은 렌더러가 영상 안에서 사진 한 장을 보여주는 시간과 같아
    (표시 시간은 지정할 수 없다) 삽입 클립만 튀지 않는다. 영상 길이는 그만큼
    늘어난다. 사진은 화면을 꽉 채우도록 비율 유지 확대 후 중앙 크롭한다.
    """
    reels = _load_ready_reels(db, reels_idx, user_idx)
    work_dir = _edit_workspace()
    try:
        source = _download_source(reels.url, work_dir)
        info = _ffprobe_video(source)
        duration = float(info["duration"])
        at = float(at_seconds)
        photo = INSERT_PHOTO_SECONDS
        if at < 0 or at > duration:
            raise BadRequestException(f"삽입 시점은 0 ~ 영상 길이({duration:.1f}초) 사이여야 합니다.")

        suffix = Path(image_filename or "").suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            raise BadRequestException("이미지 파일이 아닙니다 (jpg/png/webp 등).")
        if not image_bytes:
            raise BadRequestException("이미지 파일이 비어 있습니다.")

        width, height = int(info["width"]), int(info["height"])
        fps = str(info["fps"])
        sample_rate = int(info["sample_rate"])
        image_path = work_dir / f"insert{suffix}"
        image_path.write_bytes(image_bytes)

        # 본편을 at 기준으로 나누고(경계에 붙으면 한쪽만) 사이에 사진 클립을 끼운다.
        eps = 0.05
        head = at > eps
        tail = at < duration - eps

        # 사진 클립: 화면을 꽉 채우게 확대 후 중앙 크롭, 본편과 같은 fps/SAR 로 정규화.
        filters = [
            f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps={fps},"
            f"trim=duration={photo:.3f},setpts=PTS-STARTPTS[vp]"
        ]
        video_labels: list[str] = []
        audio_labels: list[str] = []
        has_audio = bool(info["has_audio"])
        # concat 은 세그먼트 포맷이 같아야 하므로 오디오는 전부 동일 포맷으로 정규화.
        aformat = f"aformat=sample_fmts=fltp:sample_rates={sample_rate}:channel_layouts=stereo"

        if head:
            filters.append(f"[0:v]trim=end={at:.3f},setpts=PTS-STARTPTS[v0]")
            video_labels.append("[v0]")
            if has_audio:
                filters.append(f"[0:a]atrim=end={at:.3f},asetpts=PTS-STARTPTS,{aformat}[a0]")
                audio_labels.append("[a0]")
        video_labels.append("[vp]")
        if has_audio:
            # 사진 구간은 무음 (anullsrc 입력 [2]).
            filters.append(f"[2:a]atrim=duration={photo:.3f},{aformat}[ap]")
            audio_labels.append("[ap]")
        if tail:
            filters.append(f"[0:v]trim=start={at:.3f},setpts=PTS-STARTPTS[v1]")
            video_labels.append("[v1]")
            if has_audio:
                filters.append(f"[0:a]atrim=start={at:.3f},asetpts=PTS-STARTPTS,{aformat}[a1]")
                audio_labels.append("[a1]")

        segment_count = len(video_labels)
        inputs = [
            "-i", str(source),
            "-loop", "1", "-t", f"{photo + 0.5:.3f}", "-i", str(image_path),
        ]
        maps = ["-map", "[v]"]
        audio_args: list[str] = []
        if has_audio:
            inputs += ["-f", "lavfi", "-t", f"{photo:.3f}", "-i",
                       f"anullsrc=r={sample_rate}:cl=stereo"]
            interleaved = "".join(v + a for v, a in zip(video_labels, audio_labels))
            filters.append(f"{interleaved}concat=n={segment_count}:v=1:a=1[v][a]")
            maps += ["-map", "[a]"]
            audio_args = ["-c:a", "aac", "-b:a", "192k"]
        else:
            filters.append(f"{''.join(video_labels)}concat=n={segment_count}:v=1:a=0[v]")

        target = work_dir / "insert.mp4"
        started = time.perf_counter()
        _run_ffmpeg([
            *inputs,
            "-filter_complex", ";".join(filters),
            *maps, *_EDIT_VIDEO_ARGS, *audio_args,
            str(target),
        ])
        return _edit_result(db, reels, started, target)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 내 영상 직접 업로드 — 렌더를 거치지 않고 완성 영상을 그대로 릴스로 등록한다.
# --------------------------------------------------------------------------- #
# 업로드 상한. **1차 방어선은 nginx 의 client_max_body_size(100M, deploy.yml)** 다 —
# FastAPI 는 엔드포인트가 돌기 전에 멀티파트를 통째로 파싱해 임시 파일로 흘리므로
# (routing.py: await request.form() → solve_dependencies), 앱 코드가 첫 바이트를 보는
# 시점엔 이미 디스크를 썼다. 여기 값은 그 뒤의 2차 방어(우리가 만드는 사본을 끊는다)라
# 프록시 한도와 같게 둔다 — 더 크면 사용자에게 우리 400 대신 nginx 413 이 뜬다.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


def _save_upload(file_obj, target: Path) -> None:
    """업로드 스트림을 임시 파일로 흘려 담는다 — 통째로 메모리에 올리지 않는다."""
    written = 0
    with target.open("wb") as out:
        while chunk := file_obj.read(DOWNLOAD_CHUNK_BYTES):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                raise BadRequestException(
                    f"영상이 너무 큽니다 (최대 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB)."
                )
            out.write(chunk)
    if not written:
        raise BadRequestException("빈 파일입니다.")


def _upload_content_type(filename: str) -> str:
    """업로드 파일명 확장자로 content-type 을 정한다. 영상이 아니면 400.

    확장자를 mp4 로 통일하지 않는 이유: mov/webm 을 video/mp4 로 올리면 브라우저가
    재생을 거부한다. mp4 재인코딩은 업로드마다 ffmpeg 이 도는 값이라 하지 않는다.
    """
    suffix = Path(filename or "").suffix.lower()
    content_type = mimetypes.guess_type(f"x{suffix}")[0] or ""
    if not content_type.startswith("video/"):
        raise BadRequestException(f"영상 파일이 아닙니다: {filename or '(파일명 없음)'}")
    return content_type


def upload_reels(
    db: Session, user_idx: int, filename: str, file_obj, title: str | None
) -> ReelsUploadResponse:
    """사용자가 직접 만든 영상을 릴스로 등록한다 (렌더 없음 → 진행률 폴링도 없다).

    렌더 경로와 달리 자리표 행(PENDING_REELS_URL)을 미리 만들지 않는다 — 업로드는
    요청 안에서 끝나 reels_idx 를 앞당겨 발급할 이유가 없고, 실패하면 남길 행도 없다.
    지역 태그(region)는 좌표를 알 수 없어 null 이다(홈 카드가 지역 핀을 숨긴다).
    """
    content_type = _upload_content_type(filename)
    if shutil.which("ffprobe") is None:  # 서버 설정 문제 — 아래 400 번역에 섞이면 안 된다
        raise ExternalServiceException("ffprobe를 찾을 수 없습니다 (PATH 확인).")

    work_dir = _edit_workspace()
    try:
        source = work_dir / f"upload{Path(filename).suffix.lower()}"
        _save_upload(file_obj, source)
        try:
            info = _ffprobe_video(source)
        except (ExternalServiceException, ValueError, subprocess.TimeoutExpired) as error:
            # ffprobe 가 있는데 실패 = 서버가 아니라 사용자 파일 문제라 전부 400 이다.
            # ValueError: stdout 이 JSON 이 아니거나(JSONDecodeError) width·duration 이
            # 숫자로 안 떨어질 때. TimeoutExpired: 60초 안에 못 읽을 때.
            raise BadRequestException(
                "영상을 읽을 수 없습니다 (손상됐거나 지원하지 않는 형식)."
            ) from error
        duration = float(info["duration"])
        if duration <= 0 or not info["width"]:
            raise BadRequestException("영상 트랙이 없는 파일입니다.")

        url = gcs.upload_file(f"reels/{uuid.uuid4().hex}{source.suffix}", source, content_type)
        # 인트로가 없으니 앞부분에서 뽑되, 3.5초보다 짧은 영상이면 중간 지점에서.
        thumbnail_url = _publish_thumbnail(source, min(THUMBNAIL_AT_SECONDS, duration / 2))
        try:
            reels = reels_dao.create(
                db, user_idx=user_idx, url=url, title=_clean_title(title)
            )
            reels_dao.update_url(db, reels, url, thumbnail_url)  # 썸네일까지 한 번에
            db.commit()
        except Exception:
            db.rollback()
            _delete_object_quietly(url, "고아 영상 객체")
            _delete_object_quietly(thumbnail_url, "고아 썸네일")
            raise
        return ReelsUploadResponse(
            reels_idx=reels.reels_idx,
            url=url,
            thumbnail_url=thumbnail_url,
            title=reels.title,
            duration_seconds=round(duration, 2),
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 렌더 작업(job) 관리 — 진행률 조회를 위해 비동기로 돌린다.
#
# 렌더 시작 API 는 **렌더 시작 시점에 만들어 둔 릴스의 reels_idx** 를 즉시 반환하고,
# 렌더 서브프로세스는 데몬 스레드에서 stdout 을 한 줄씩 읽으며 진행률을 갱신한다.
# 클라이언트는 GET /render/{reels_idx} 로 폴링한다 — 진행률 조회·완료 후 영상
# 다운로드·편집이 모두 같은 키(reels_idx)로 돈다(별도 job_id 없음).
#
# 진행률 레지스트리는 인메모리라 서버 재시작(--reload 포함) 시 사라진다. 그 때는
# 릴스 행만 보고 상태를 만들어 답한다(_status_from_reels).
# --------------------------------------------------------------------------- #
_jobs: dict[int, dict] = {}
_jobs_lock = threading.Lock()

# 레지스트리에 남길 job 수 상한. 등록만 하고 지우지 않으면 재시작 전까지 계속 쌓인다.
# 끝난 job 을 바로 버리지 않는 이유는 **실패 사유**다 —
# 렌더가 실패하면 자리표 릴스 행이 삭제되므로, 여기서 지우면 폴링하던 클라이언트가
# error 대신 404 를 받는다. 그래서 '오래된 끝난 job 부터' 덜어낸다.
_MAX_JOBS = 200

# 렌더가 끝나기 전 릴스 행의 url 자리표. url 은 NOT NULL 이라 NULL 을 못 쓴다.
# 이 값인 행은 "렌더 중"이라 추천 피드에서 빠지고(reels_dao.get_random_reels)
# 다운로드·편집도 거부한다(_require_ready).
PENDING_REELS_URL = ""

# job dict 에만 두고 상태 응답(_job_snapshot)에서는 빼는 내부 필드.
_INTERNAL_JOB_KEYS = ("started_at", "user_idx", "job_dir", "cover_path", "intro_source", "title")

# 사용자에게 내보내는 실패 문구. **렌더 stdout·예외 메시지를 그대로 실어 보내지 않는다** —
# 서버 경로·Modal 내부 로그가 앱 화면까지 나간다. 원인은 서버 로그에만 남긴다.
FAILED_MESSAGE = "영상 만들기에 실패했습니다. 잠시 후 다시 시도해 주세요."
INTERRUPTED_MESSAGE = "서버 재시작으로 영상 만들기가 중단됐습니다. 다시 시도해 주세요."

# 부팅 스윕이 지우는 잔여물의 나이 기준. 살아 있는 렌더의 입력을 지우지 않으려고 나이로
# 자른다 — 렌더는 30분 하드 캡이라 하루 지난 디렉터리가 진행 중일 수는 없다.
# output/ 은 GCS 업로드에 실패한 영상을 회수용으로 남기는 곳이라(_render_job) 더 넉넉히 둔다.
STALE_UPLOAD_HOURS = 24
STALE_OUTPUT_DAYS = 7

# render_video.py 가 15프레임마다 찍는 "[perf:frame] 000060/000127 ..." 라인.
_FRAME_PROGRESS_RE = re.compile(r"\[perf:frame\]\s*(\d+)/(\d+)")
# 프레임 이후 후처리 마커 → 해당 시점의 percent.
_POSTPROCESS_MARKS = [
    ("렌더링 완료", 92.0, "영상 인코딩 마무리"),
    ("BGM 합성 완료", 95.0, "후처리(BGM)"),
    ("인트로 합성 완료", 97.0, "후처리(인트로)"),
    ("아웃트로 합성 완료", 98.0, "후처리(아웃트로)"),
]


def _validate_render_options(theme: str) -> str:
    """테마 옵션을 정규화·검증한다 (400 은 여기서 동기적으로 발생)."""
    theme = (theme or "default").lower().strip()
    if theme not in ALLOWED_THEMES:
        raise BadRequestException(f"알 수 없는 테마: {theme}")
    return theme


def _clean_title(title: str | None) -> str | None:
    """릴스 제목 정규화 — 공백만이면 None(제목 없음)으로 본다. 길이는 폼에서 100자로 막는다."""
    title = (title or "").strip()
    return title or None


def _region_of_trip(track_points: list[dict[str, object]]) -> str | None:
    """경로의 지역 태그(홈 카드용). 카카오 호출이 실패하면 None.

    출발 지점에서 가장 멀리 간 지점을 여행지로 본다 — 첫 지점은 집·출발역일 때가
    많아 "서울"이 붙어버린다. 태그는 부가 정보라 실패(키 미설정·네트워크)를 흡수하고
    렌더를 막지 않는다.
    """
    # ponytail: 최다 방문 지역이 아니라 최장거리 1지점. 경유가 많은 여행에서 태그가
    # 어긋나면 지점별로 세서 최빈값을 쓰되, 지오코딩 호출이 지점 수만큼 늘어난다.
    if not track_points:
        return None
    origin = track_points[0]
    farthest = max(
        track_points,
        key=lambda p: _haversine_km(
            float(origin["latitude"]), float(origin["longitude"]),
            float(p["latitude"]), float(p["longitude"]),
        ),
    )
    try:
        return kakao_local.region_of(
            float(farthest["latitude"]), float(farthest["longitude"])
        )
    except Exception as error:
        # 좌표는 사용자 위치라 로그에 남기지 않는다 — 실패 원인 판별엔 예외 종류면
        # 충분하다(키 미설정·타임아웃·HTTP 오류가 서로 다른 타입으로 온다).
        logger.warning("릴스 지역 태그 조회 실패(무시): %s", type(error).__name__)
        return None


def sweep_stale_renders() -> None:
    """재시작으로 중단된 렌더가 남긴 것들을 치운다 (lifespan 부팅 시 1회).

    렌더는 데몬 스레드라 프로세스가 죽으면 _run_render_job 의 finally 가 못 돈다.
    배포가 `systemctl restart` 라 렌더 중 재시작은 드문 일이 아니고, 그때마다
    영상 없는 자리표 릴스 행과 업로드 사진 디렉터리가 그대로 남는다.

    1. 자리표 릴스 행을 지우고(_discard_pending_reels — FK 걸리면 소프트 삭제로 물러섬),
       폴링하던 클라이언트가 404 대신 사유를 받도록 실패한 job 을 레지스트리에 남긴다.
    2. uploads/ 와 output/ 의 오래된 잔여물을 지운다(나이 기준은 STALE_* 상수 주석 참고).

    정리는 부가 작업이라 어떤 실패도 부팅을 막지 않는다 — 전부 삼키고 로그만 남긴다.

    ponytail: 자리표 행을 지우는 기준이 "url 이 비었다" 뿐이라 **다중 워커에서 워커
    하나만 되살아나면** 다른 워커가 지금 돌리는 렌더의 행까지 지운다. 그래도 결과가
    깨지지는 않는다 — 그 렌더는 update_url_if_alive 가 0건을 받고, _publish_reels_video
    가 방금 올린 버킷 객체를 되돌린다. 손해는 GPU 시간과 그 한 편의 재시도뿐이다.
    다중 워커로 갈 일이 생기면 lease·소유권을 만들 것 없이 list_pending 에 나이 조건만
    걸면 된다 — Reels 는 BaseModel 이라 created_at 이 이미 있고, 렌더는 30분 하드
    캡이라 그보다 넉넉한 값이면 살아 있는 렌더를 건드리지 않는다.
    """
    try:
        from databases.database import SessionLocal

        db = SessionLocal()
        try:
            stale = [(r.reels_idx, r.user_idx) for r in reels_dao.list_pending(db, PENDING_REELS_URL)]
        finally:
            db.close()

        for reels_idx, user_idx in stale:
            _discard_pending_reels(reels_idx)
            with _jobs_lock:
                _jobs[reels_idx] = _new_job(
                    reels_idx, user_idx,
                    status="failed", phase="중단됨", error=INTERRUPTED_MESSAGE,
                )
        if stale:
            # 첫 스윕은 그동안 쌓인 걸 한꺼번에 만나므로 레지스트리 상한을 넘길 수 있다.
            with _jobs_lock:
                _trim_jobs_locked()
            logger.info("재시작으로 중단된 렌더 정리: %d건", len(stale))

        # ponytail: 나이로만 자른다. 이 프로세스가 아는 job 을 빼는 게 정확하지만, 그러면
        # 다중 워커에서 남의 워커가 렌더 중인 디렉터리를 지우게 된다.
        upload_cutoff = time.time() - STALE_UPLOAD_HOURS * 3600
        for path in UPLOADS_DIR.glob("*"):
            if path.is_dir() and path.stat().st_mtime < upload_cutoff:
                shutil.rmtree(path, ignore_errors=True)

        output_cutoff = time.time() - STALE_OUTPUT_DAYS * 86400
        for path in OUTPUT_DIR.glob("*.mp4"):
            if path.stat().st_mtime < output_cutoff:
                path.unlink(missing_ok=True)
    except Exception:
        logger.warning("중단된 렌더 정리 실패(무시)", exc_info=True)


def _new_job(reels_idx: int, user_idx: int | None, **overrides) -> dict:
    """job 레지스트리 항목의 기본 모양 — 진행률 응답(_job_snapshot)이 읽는 키를 한곳에 둔다.

    렌더를 띄우는 _spawn_render_job 과 부팅 스윕(sweep_stale_renders)이 같이 쓴다.
    스윕이 만드는 항목은 돌 스레드가 없으므로 status·error 를 덮어쓴다.

    user_idx 가 None 인 건 스윕이 만난 옛 익명 릴스뿐이다(Reels.user_idx 는 nullable).
    소유 확인(get_render_job)이 JWT 의 int 와 비교하므로 그 항목은 아무에게도 안 잡힌다.
    """
    return {
        "user_idx": user_idx,
        "job_dir": None,
        # 대표 사진으로 미리 만들어 둔 표지 JPEG (job_dir 안). 없으면 완성 영상에서 뽑는다.
        "cover_path": None,
        # 인트로 2초를 그릴 원본 (사진 바이트 파일 또는 클립 경로). 제목은 여기 같이 쓴다.
        "intro_source": None,
        "title": None,
        "reels_idx": reels_idx,
        "reels_url": None,
        "status": "running",
        "phase": "렌더 준비 중",
        "percent": 0.0,
        "frame": 0,
        "total_frames": None,
        "started_at": time.time(),
        "elapsed_seconds": 0.0,
        "eta_seconds": None,
        "engine": _engine(),
        "theme": "default",
        "bgm": None,
        "video_url": None,
        "error": None,
        **overrides,
    }


def _spawn_render_job(
    db: Session,
    travel_data_path: Path,
    bgm_path: Path | None,
    theme: str,
    user_idx: int,
    title: str | None = None,
    region: str | None = None,
    max_video_seconds: float | None = None,
    cover_path: Path | None = None,
    intro_source: Path | None = None,
) -> dict[str, object]:
    """릴스 행을 먼저 등록하고, 렌더 서브프로세스를 백그라운드 스레드로 띄운다.

    릴스는 url 이 PENDING_REELS_URL 인 "렌더 중" 상태로 먼저 만들어지고, 그
    reels_idx 가 곧 진행률 조회 키다(작성자는 렌더 요청자 user_idx). 렌더가 끝나면
    결과 영상을 GCS 버킷(reels/)에 올려 url 을 채우고, 실패하면 그 행을 DB 에서
    지운다(_discard_pending_reels) — 영상 없는 릴스가 남지 않게.
    """
    reels = reels_dao.create(
        db, user_idx=user_idx, url=PENDING_REELS_URL, title=_clean_title(title),
        region=region,
    )
    db.commit()

    # 표지 인트로를 붙일 수 있으면 TRAILER 인트로는 빼고(둘 다 붙이지 않는다), 못 만들었으면
    # 예전처럼 TRAILER 를 붙인다 — 인트로가 통째로 없는 영상이 나오지 않게.
    command = _build_command(
        travel_data_path, theme, max_video_seconds, trailer_intro=intro_source is None
    )
    job = _new_job(
        reels.reels_idx,
        user_idx,
        intro_source=str(intro_source) if intro_source else None,
        title=_clean_title(title),
        # 렌더 입력(업로드 사진·travel_data.json)이 담긴 디렉터리 — 끝나면 지운다.
        job_dir=str(travel_data_path.parent),
        # 표지는 job_dir 안에 둔다 — 렌더가 실패하면 디렉터리째 지워져 고아 GCS 객체가
        # 남지 않는다(성공했을 때만 _publish_reels_video 가 올린다).
        cover_path=str(cover_path) if cover_path else None,
        theme=theme,
        bgm=bgm_path.name if bgm_path else None,
    )
    with _jobs_lock:
        _jobs[job["reels_idx"]] = job
    threading.Thread(
        target=_run_render_job, args=(job, command), daemon=True
    ).start()
    return _job_snapshot(job)


# --------------------------------------------------------------------------- #
# 사진만으로 렌더링 — EXIF 의 GPS/촬영시각으로 경로를 자동 구성한다.
# --------------------------------------------------------------------------- #
# 연속 촬영 사진을 한 지점으로 묶는 거리 기준(km). 묶음의 첫 사진 위치가 기준점이 되고,
# 기준점에서 이 거리 미만인 사진은 카메라 이동 없이 그 자리에서 순서대로 보여준다.
# 이 거리 이상 떨어진 사진이 나와야 다음 지점으로 이동한다.
PHOTO_CLUSTER_KM = 1.0

# 렌더 한 번에 받을 사진 장수 상한. 장수만큼 지점이 늘어 렌더 시간도 같이 길어지므로
# 30분 하드 캡(RENDER_TIMEOUT_SECONDS) 안에 끝날 만큼으로 둔다. 여행 사진 첨부의
# MAX_TRAVEL_IMAGES(20)보다 넉넉한 건 이쪽이 '여행 전체'를 한 번에 올리는 입구라서다.
MAX_RENDER_PHOTOS = 30
# 사진 1장 크기 상한. 프로필·대표 사진과 같은 값을 쓴다(gcs.MAX_IMAGE_BYTES, 10MB).
# ponytail: 바이트 총량의 실제 천장은 nginx client_max_body_size(100M)다 — 여기 둘은
# 초과 요청을 413 대신 사유가 담긴 400 으로 끊고, 장수를 유한하게 묶는 몫이다.
MAX_RENDER_PHOTO_BYTES = gcs.MAX_IMAGE_BYTES


def _extract_photo_meta(content: bytes) -> dict[str, object] | None:
    """사진 EXIF 에서 GPS 좌표·촬영 시각을 뽑는다. GPS 가 없으면 None.

    반환: {"latitude", "longitude", "taken": datetime|None}
    """

    def to_decimal(dms, ref: str) -> float:
        degrees, minutes, seconds = (float(v) for v in dms)
        decimal = degrees + minutes / 60 + seconds / 3600
        return -decimal if ref in ("S", "W") else decimal

    try:
        with Image.open(io.BytesIO(content)) as image:
            exif = image.getexif()
            if not exif:
                return None
            gps = exif.get_ifd(0x8825)  # GPSInfo IFD
            # 1/2: 위도 기준·값, 3/4: 경도 기준·값
            if 2 not in gps or 4 not in gps:
                return None
            latitude = to_decimal(gps[2], str(gps.get(1, "N")))
            longitude = to_decimal(gps[4], str(gps.get(3, "E")))

            taken = None
            raw = exif.get_ifd(0x8769).get(0x9003) or exif.get(0x0132)  # DateTimeOriginal | DateTime
            if raw:
                try:
                    taken = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    taken = None
            return {"latitude": round(latitude, 7), "longitude": round(longitude, 7), "taken": taken}
    except Exception:
        # 손상된 이미지/EXIF 는 GPS 없음으로 취급한다.
        return None


# 사진과 섞어 올리는 짧은 영상(클립). 렌더러의 render_video.CLIP_EXTENSIONS·CLIP_MAX_SECONDS
# 와 같은 값이어야 한다 — 그 모듈은 최상위에서 playwright 를 임포트해 서버가 가져다 쓸 수 없다.
RENDER_CLIP_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
RENDER_CLIP_SECONDS = 5.0
# 영상 한 편에 들어가는 클립 길이 합(클립마다 쓰이는 앞 5초 기준). 사진·클립 시간은 60초 상한에서
# 줄지 않고 지도 이동만 줄어드니, 클립이 많으면 이동이 순간이동처럼 된다.
MAX_RENDER_CLIP_TOTAL_SECONDS = 15.0
# ISO 6709 "+37.5547+126.9706+012.345/" — 안드로이드 location·아이폰 quicktime 태그 공통 형식.
_ISO6709_RE = re.compile(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)")


def _extract_clip_meta(path: Path, filename: str) -> dict[str, object]:
    """영상 태그에서 GPS·촬영 시각·쓸 길이를 뽑는다. 영상으로 못 읽으면 400.

    반환: {"latitude", "longitude" (GPS 없으면 둘 다 None), "taken", "seconds"}
    taken 은 사진 EXIF 와 같은 축(한국 현지 시각, 시간대 없음)으로 맞춘다 — 안드로이드
    creation_time 은 UTC 라 그대로 두면 촬영순 정렬에서 9시간 어긋난다.
    seconds 는 렌더러가 실제로 쓰는 길이 = min(영상 길이, RENDER_CLIP_SECONDS).
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:  # 서버 설정 문제 — 아래 '못 읽는 파일' 400 에 섞이면 안 된다
        raise ExternalServiceException("ffprobe를 찾을 수 없습니다 (PATH 확인).")
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error",
             "-show_entries", "format=duration:format_tags:stream=codec_type",
             "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        info = json.loads(result.stdout or "{}")
        duration = float(info.get("format", {}).get("duration") or 0)
    except (ValueError, subprocess.TimeoutExpired):
        duration, info = 0.0, {}
    has_video = any(s.get("codec_type") == "video" for s in info.get("streams", []))
    if duration <= 0 or not has_video:
        raise BadRequestException(f"영상을 읽을 수 없습니다: {filename or '(파일명 없음)'}")

    tags = info.get("format", {}).get("tags") or {}
    latitude = longitude = None
    match = _ISO6709_RE.match(
        tags.get("com.apple.quicktime.location.ISO6709") or tags.get("location") or ""
    )
    if match:
        lat, lon = float(match[1]), float(match[2])
        if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0.0, 0.0):
            latitude, longitude = round(lat, 7), round(lon, 7)

    taken = None
    raw = tags.get("com.apple.quicktime.creationdate") or tags.get("creation_time")
    if raw:
        try:
            parsed = datetime.fromisoformat(raw)
            taken = parsed.astimezone(KST).replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            taken = None
    return {
        "latitude": latitude, "longitude": longitude, "taken": taken,
        "seconds": min(duration, RENDER_CLIP_SECONDS),
    }


def _save_render_clip(
    job_dir: Path, stem: str, filename: str, stream: BinaryIO
) -> tuple[str, dict[str, object]]:
    """클립을 job 디렉터리에 저장하고 (렌더러 기준 상대 경로, 메타)를 반환한다.

    원본(최대 100MB)을 그대로 두지 않고 **앞 5초만, 소리 없이** 스트림 카피로 잘라 둔다 —
    렌더 입력은 통째로 Modal 에 실려 가므로 안 쓰는 뒷부분까지 보낼 이유가 없다.
    재인코딩이 아니라 몇 초 안 걸린다. 메타는 잘라내기 전 원본에서 읽는다.
    """
    suffix = Path(filename).suffix.lower()
    source = job_dir / f"{stem}_src{suffix}"
    _save_upload(stream, source)
    meta = _extract_clip_meta(source, filename)
    target = job_dir / f"{stem}{suffix}"
    _run_ffmpeg([
        "-i", str(source), "-t", f"{RENDER_CLIP_SECONDS:.3f}",
        "-map", "0:v:0", "-c", "copy", str(target),
    ])
    source.unlink(missing_ok=True)
    return target.relative_to(VIDEO_MAKER_DIR).as_posix(), meta


def _group_render_items(
    items: list[tuple[str, dict]], sort_by_time: bool
) -> list[tuple[list[str], dict]]:
    """GPS 있는 항목마다 (보여줄 파일 목록, meta)를 만들고, GPS 없는 영상은 이웃 지점에 붙인다.

    GPS 없는 사진은 items 에 애초에 없다(기존 규칙대로 제외). 영상은 사용자가 골라 넣은
    것이라 말없이 빼지 않고 붙일 곳을 찾는다:
      - 순서 지정: 바로 앞 항목의 지점. 첫 GPS 항목보다 앞선 영상은 그 항목 앞에 붙는다.
      - 촬영순: 촬영 시각이 가장 가까운 항목의 지점. 시각이 없으면 붙일 곳이 없어 뺀다.
    """
    if not sort_by_time:
        stops: list[tuple[list[str], dict]] = []
        pending: list[str] = []
        for rel, meta in items:
            if meta["latitude"] is not None:
                stops.append(([*pending, rel], meta))
                pending = []
            elif stops:
                stops[-1][0].append(rel)
            else:
                pending.append(rel)
        return stops

    # 촬영 시각 순 정렬 — 시각 없는 항목은 업로드 순서를 유지한 채 뒤로 보낸다.
    stops = [([rel], meta) for rel, meta in items if meta["latitude"] is not None]
    stops.sort(key=lambda stop: (stop[1]["taken"] is None, stop[1]["taken"] or datetime.min))
    timed = [stop for stop in stops if stop[1]["taken"] is not None]
    loose = sorted(
        (item for item in items if item[1]["latitude"] is None and item[1]["taken"] is not None),
        key=lambda item: item[1]["taken"],
    )
    for rel, meta in loose:
        if timed:
            # ponytail: 붙는 자리는 항상 그 지점 사진들 뒤다 — 사진보다 먼저 찍은 영상도 뒤에 나온다
            min(timed, key=lambda stop: abs(stop[1]["taken"] - meta["taken"]))[0].append(rel)
    return stops


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


# --------------------------------------------------------------------------- #
# 표지(썸네일) — 사용자가 고른 대표 사진에 청량한 보정 + 제목을 얹는다.
#
# 완성 영상에서 프레임을 뽑던 기존 경로(_publish_thumbnail)를 대신한다. 만드는 건
# 요청 시점이지만 **올리는 건 렌더가 끝난 뒤**다 — 실패한 렌더의 표지가 버킷에 남지
# 않게(job_dir 이 통째로 지워진다).
# --------------------------------------------------------------------------- #
COVER_FILE_NAME = "cover.jpg"
# 표지로 만든 인트로가 화면에 머무는 시간(초). TRAILER 인트로(~3.6초)를 대신하므로
# 앞단이 오히려 짧아진다 — 제목 한 줄을 읽기엔 2초면 넉넉하다.
INTRO_SECONDS = 2.0


def _resolve_cover_index(cover_index: int | None, count: int) -> int:
    """1-based 대표 번호를 검증해 돌려준다. 안 주면 1번(첫 번째로 보낸 것)."""
    index = 1 if cover_index is None else cover_index
    if not 1 <= index <= count:
        raise BadRequestException(f"대표 사진 번호는 1 ~ {count} 사이여야 합니다.")
    return index


def _first_frame_bytes(video_path: Path) -> bytes | None:
    """영상 첫 프레임을 JPEG 바이트로 뽑는다 (대표 번호가 영상을 가리킬 때). 실패하면 None."""
    frame = video_path.with_name(f"{video_path.stem}_first.jpg")
    try:
        _run_ffmpeg(["-i", str(video_path), "-frames:v", "1", "-q:v", "3", str(frame)])
        return frame.read_bytes()
    except Exception:
        logger.warning("대표 영상의 첫 프레임 추출 실패(무시): %s", video_path.name)
        return None
    finally:
        frame.unlink(missing_ok=True)


def _write_cover(job_dir: Path, source: bytes | None, title: str | None) -> Path | None:
    """대표 사진 바이트로 표지를 만들어 job_dir 에 저장한다. 못 만들면 None.

    **원본 바이트도 함께 남긴다**(cover_src.bin) — 인트로는 본편 해상도로 다시 그려야
    하는데 그 해상도는 렌더가 끝나야 알 수 있고, 540x960 썸네일을 늘리면 글씨가 뭉갠다.
    """
    if not source:
        return None
    cover = cover_image.build_cover(source, title)
    if cover is None:
        return None
    path = job_dir / COVER_FILE_NAME
    path.write_bytes(cover)
    (job_dir / COVER_SOURCE_NAME).write_bytes(source)
    return path


def _intro_source_path(
    job_dir: Path, cover_path: Path | None, clip_path: Path | None = None
) -> Path | None:
    """인트로를 그릴 원본 — 대표가 영상이면 그 클립, 사진이면 표지가 쓴 원본 바이트.

    표지를 못 만들었으면 None 이고, 그 렌더는 TRAILER 인트로가 붙는다.
    """
    if cover_path is None:
        return None
    return clip_path or (job_dir / COVER_SOURCE_NAME)


# --------------------------------------------------------------------------- #
# 인트로 — 표지를 2초짜리 클립으로 만들어 본편 앞에 붙인다.
#
# TRAILER 글자 마스크 인트로를 **대신한다**(표지를 못 만든 렌더만 TRAILER 가 붙는다).
# 본편을 재인코딩하지 않으려고 본편과 같은 코덱 파라미터로 클립만 인코딩한 뒤 stream
# copy concat 한다 — intro_video 의 검증된 함수를 그대로 쓴다. 그 모듈은 Pillow 와
# subprocess 만 쓰므로(playwright 없음) 이 서버에서 바로 import 된다.
# --------------------------------------------------------------------------- #
COVER_SOURCE_NAME = "cover_src.bin"


def _intro_video():
    """services/videoMaker/intro_video.py 를 로드한다 (그 디렉터리는 패키지가 아니다).

    Pillow·subprocess 만 쓰는 모듈이라 playwright 없이 이 서버에서 그대로 돈다.
    sys.path 에는 **뒤에** 붙인다 — 앞에 끼우면 videoMaker 의 파일들이 같은 이름의
    표준 모듈을 가려 버린다.
    """
    if str(VIDEO_MAKER_DIR) not in sys.path:
        sys.path.append(str(VIDEO_MAKER_DIR))
    import intro_video  # noqa: PLC0415

    return intro_video


def _encode_intro_clip(
    source_args: list[str],
    filters: str,
    target: Path,
    info: dict[str, object],
    timescale: int | None,
    audio_spec: tuple[int, int] | None,
) -> None:
    """본편과 같은 코덱 파라미터로 2초 인트로 클립을 인코딩한다 (stream copy concat 용).

    타임스케일이 어긋나면 concat 뒤 **본편 재생 속도가 틀어진다**. 본편에 BGM 이 있으면
    같은 스펙의 무음 트랙을 넣어야 concat 이 오디오까지 이어 붙는다.
    """
    args = list(source_args)
    maps = ["-map", "[v]"]
    audio_args: list[str] = []
    if audio_spec is not None:
        rate, channels = audio_spec
        layout = "stereo" if channels >= 2 else "mono"
        args += ["-f", "lavfi", "-t", f"{INTRO_SECONDS:.3f}",
                 "-i", f"anullsrc=r={rate}:cl={layout}"]
        maps += ["-map", f"{source_args.count('-i')}:a"]  # 무음 트랙은 마지막 입력
        audio_args = ["-c:a", "aac", "-b:a", "128k", "-ar", str(rate), "-ac", str(channels)]
    args += ["-filter_complex", filters, *maps,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(info["fps"])]
    if timescale:
        args += ["-video_track_timescale", str(timescale)]
    _run_ffmpeg([*args, *audio_args, "-movflags", "+faststart", str(target)])


def _prepend_cover_intro(video_path: Path, job: dict) -> None:
    """완성 영상 앞에 표지 인트로 2초를 붙인다 (in place). 실패해도 본편은 그대로 둔다.

    대표가 **사진**이면 썸네일과 같은 보정·제목을 본편 해상도로 다시 그려 정지 2초로,
    **영상**이면 그 영상의 앞 2초에 제목만 얹어(보정 없음) 붙인다. 2초보다 짧은 영상은
    마지막 프레임을 늘려 채운다(tpad) — 클립이 5초로 잘려 있어 보통은 그냥 앞 2초다.
    """
    source_path = job.get("intro_source")
    if not source_path or not Path(source_path).exists():
        return
    source = Path(source_path)
    title = job.get("title") or ""
    try:
        intro_module = _intro_video()
        info = _ffprobe_video(video_path)
        width, height = int(info["width"]), int(info["height"])
        _, audio_spec, timescale = intro_module.probe_video(video_path)

        work = source.parent
        intro_path = work / "cover_intro.mp4"
        if source.suffix.lower() in RENDER_CLIP_EXTENSIONS:
            overlay = cover_image.build_title_overlay(
                width, height, title, _first_frame_bytes(source)
            )
            scale = (f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
                     f"crop={width}:{height},setsar=1,fps={info['fps']},"
                     f"tpad=stop_mode=clone:stop_duration={INTRO_SECONDS:.3f}")
            if overlay is None:  # 제목이 없거나 폰트를 못 찾은 경우 — 영상만 2초
                args = ["-t", f"{INTRO_SECONDS:.3f}", "-i", str(source)]
                filters = f"{scale},trim=duration={INTRO_SECONDS:.3f},setpts=PTS-STARTPTS[v]"
            else:
                overlay_path = work / "intro_title.png"
                overlay_path.write_bytes(overlay)
                args = ["-t", f"{INTRO_SECONDS:.3f}", "-i", str(source),
                        "-i", str(overlay_path)]
                filters = (f"{scale}[base];[base][1:v]overlay=0:0,"
                           f"trim=duration={INTRO_SECONDS:.3f},setpts=PTS-STARTPTS[v]")
        else:
            still = cover_image.build_cover(source.read_bytes(), title, size=(width, height))
            if still is None:
                return
            still_path = work / "intro_still.jpg"
            still_path.write_bytes(still)
            args = ["-loop", "1", "-t", f"{INTRO_SECONDS:.3f}", "-i", str(still_path)]
            filters = f"[0:v]setsar=1,fps={info['fps']}[v]"

        _encode_intro_clip(args, filters, intro_path, info, timescale, audio_spec)
        intro_module.concat_replace(
            shutil.which("ffmpeg"), [intro_path, video_path], video_path, work, "cover_intro"
        )
    except Exception:
        # 인트로는 부가 연출이라 여기서 실패해도 본편은 그대로 올라가야 한다.
        logger.warning("표지 인트로 합성 실패(무시): reels_idx=%s", job.get("reels_idx"))


def _publish_cover(cover_path: str | None) -> str | None:
    """미리 만들어 둔 표지를 버킷에 올린다 → 공개 URL. 없거나 실패하면 None."""
    if not cover_path:
        return None
    path = Path(cover_path)
    if not path.exists():
        return None
    try:
        return gcs.upload_bytes(
            f"{THUMBNAIL_OBJECT_PREFIX}/{uuid.uuid4().hex}.jpg",
            path.read_bytes(),
            "image/jpeg",
        )
    except Exception:
        logger.warning("릴스 표지 업로드 실패(무시): %s", path.name)
        return None


def _new_job_dir() -> Path:
    """이번 렌더 작업의 입력을 담을 uploads/ 하위 디렉터리를 만든다."""
    job_dir = UPLOADS_DIR / uuid.uuid4().hex[:12]
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def _save_render_image(job_dir: Path, stem: str, source_name: str, content: bytes) -> str:
    """이미지를 job 디렉터리에 저장하고 렌더러 기준 상대 경로(POSIX)를 반환한다."""
    suffix = Path(source_name or "").suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        suffix = ".jpg"
    saved = job_dir / f"{stem}{suffix}"
    saved.write_bytes(content)
    return saved.relative_to(VIDEO_MAKER_DIR).as_posix()


def _append_stop(
    track_points: list[dict[str, object]],
    media_points: list[dict[str, object]],
    latitude: float,
    longitude: float,
    photos: list[str],
    name: str | None = None,
    timestamp: str | None = None,
) -> None:
    """지점을 추가하거나, 직전 지점에서 PHOTO_CLUSTER_KM 미만이면 사진만 합친다.

    새 지점의 name 을 생략하면 빈 이름으로 둔다 — _name_stops 가 좌표로 채우고, 거기서도
    못 찾으면 영상에 라벨이 뜨지 않는다("지점 3" 같은 이름은 보여줄 가치가 없다).
    """
    if track_points and _haversine_km(
        float(track_points[-1]["latitude"]), float(track_points[-1]["longitude"]),
        latitude, longitude,
    ) < PHOTO_CLUSTER_KM:
        # 같은 장소의 연속 사진/일정 → 직전 지점에서 순서대로 이어 보여준다.
        media_points[-1]["photos"].extend(photos)
        return

    point: dict[str, object] = {"latitude": latitude, "longitude": longitude}
    if timestamp is not None:
        point["timestamp"] = timestamp
    track_points.append(point)
    media_points.append({
        "trackIndex": len(track_points) - 1,
        "name": (name or "").strip(),
        "photos": photos,
    })


def _name_stops(
    track_points: list[dict[str, object]], media_points: list[dict[str, object]]
) -> None:
    """이름이 빈 지점에 좌표로 찾은 장소명(관광명소 → 동네)을 채운다(제자리 수정).

    지점마다 카카오 호출이 1~2회라 병렬로 부른다 — 운영 VM 이 미국이라 순차면 지점당
    0.7초씩 쌓인다. 장소명은 부가 정보라 실패(키 미설정·네트워크)를 흡수하고 렌더를
    막지 않는다. 못 찾은 지점은 빈 이름 그대로 두어 라벨을 숨긴다.
    """
    targets = [media for media in media_points if not media["name"]]

    def lookup(media: dict[str, object]) -> str | None:
        point = track_points[int(media["trackIndex"])]
        try:
            return kakao_local.place_name_of(float(point["latitude"]), float(point["longitude"]))
        except Exception as error:
            # 좌표는 사용자 위치라 로그에 남기지 않는다(_region_of_trip 과 같은 이유).
            logger.warning("지점 장소명 조회 실패(무시): %s", type(error).__name__)
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        for media, name in zip(targets, pool.map(lookup, targets)):
            media["name"] = name or ""


def _write_travel_data(
    job_dir: Path,
    track_points: list[dict[str, object]],
    media_points: list[dict[str, object]],
    bgm: str,
    photo_labels: dict[str, str] | None = None,
) -> tuple[Path, Path | None]:
    """travel_data.json 을 job 디렉터리에 쓰고 (경로, BGM 경로|None)을 반환한다.

    photo_labels 는 사진별 장소명 {사진 경로: 이름} — 없는 사진은 지점 이름을 쓴다.
    """
    travel_data: dict[str, object] = {
        "trackPoints": track_points,
        "mediaPoints": media_points,
    }
    if photo_labels:
        travel_data["photoLabels"] = photo_labels
    bgm_path: Path | None = None
    if bgm.strip():
        bgm_path = get_bgm_path(bgm.strip())
        travel_data["bgm"] = bgm_path.relative_to(VIDEO_MAKER_DIR).as_posix()

    travel_data_path = job_dir / "travel_data.json"
    travel_data_path.write_text(
        json.dumps(travel_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return travel_data_path, bgm_path


def start_render_photos_only(
    db: Session,
    photos: list[tuple[str, BinaryIO]],
    *,
    user_idx: int,
    bgm: str = "",
    theme: str = "default",
    start_name: str = "",
    start_latitude: float | None = None,
    start_longitude: float | None = None,
    sort_by_time: bool = True,
    title: str | None = None,
    cover_index: int | None = None,
) -> dict[str, object]:
    """사진들의 EXIF(GPS·촬영시각)만으로 여행 경로 영상 렌더링을 시작한다.

    시작과 동시에 작성자가 user_idx(JWT 인증 사용자)인 릴스 행이 "렌더 중" 상태로
    만들어지고, 그 reels_idx 로 진행률을 조회한다(렌더가 끝나면 같은 행의 url 이
    채워진다).

    sort_by_time=True(기본)면 촬영 시각 순으로, False 면 촬영 시각을 무시하고
    업로드한 순서 그대로(사용자 지정 순서) 지점을 이동하며 각 지점에서 해당
    사진을 보여준다. GPS 없는 사진은 제외한다. 지점 기준점(묶음 첫 사진 위치)에서
    PHOTO_CLUSTER_KM(1km) 미만인 연속 사진은 이동 없이 그 지점에 고정해
    사진1→사진2→사진3 순으로 보여주고, 1km 이상 떨어진 사진이 나오면 그 위치로
    이동한다.

    start_latitude/longitude 를 주면 그 위치(예: 서울역)를 출발지로 삼아
    첫 사진 지점으로 이동하며 시작한다 (출발지에서는 사진 없이 라벨만 표시).

    cover_index 는 릴스 표지로 쓸 파일의 **업로드 순서(1부터)** 다. 안 주면 1번.
    그 파일에 청량한 보정과 제목을 얹어 썸네일로 쓴다 — GPS 가 없어 영상에는 못 들어가는
    사진이어도 표지로는 쓸 수 있고(사용자가 고른 건 '예쁜 사진'이지 '좌표 있는 사진'이
    아니다), 영상이면 첫 프레임을 뽑는다. 표지를 못 만들면 예전처럼 완성 영상에서
    프레임을 뽑는다.

    사진은 바이트가 아니라 **스트림**으로 받아 한 장씩 읽고 job 디렉터리로 흘려보낸다 —
    전량을 메모리에 올리지 않으려는 것이다(travel_service.add_images 와 같은 처리).
    """
    theme = _validate_render_options(theme)

    # 시작 위치 검증 — 위도/경도는 함께 와야 한다.
    if (start_latitude is None) != (start_longitude is None):
        raise BadRequestException("시작 위치는 위도/경도를 함께 보내야 합니다.")
    if start_latitude is not None and not (
        -90 <= start_latitude <= 90 and -180 <= start_longitude <= 180
    ):
        raise BadRequestException("시작 위치 좌표가 올바르지 않습니다.")
    # 장수 검증은 **읽기 전에** 끝낸다 — 초과분의 바이트를 아예 만지지 않으려고.
    if len(photos) > MAX_RENDER_PHOTOS:
        raise BadRequestException(
            f"사진·영상은 합쳐서 한 번에 {MAX_RENDER_PHOTOS}개까지 올릴 수 있습니다."
        )
    if len(photos) < 2:
        raise BadRequestException("사진·영상이 2개 이상 필요합니다.")
    cover_at = _resolve_cover_index(cover_index, len(photos))

    job_dir = _new_job_dir()
    try:
        # 한 장씩 읽어 바로 디스크로 넘긴다. 다음 회차에서 content 가 새로 묶이며 직전 장은
        # 풀리므로 동시에 드는 건 1장뿐이다(영상은 청크로 흘려 저장한다). 파일명의 순번은
        # 업로드 순서(고유 이름을 만드는 용도)일 뿐, 영상의 이동 순서는 _group_render_items 가 정한다.
        items: list[tuple[str, dict]] = []
        clip_seconds = 0.0
        cover_source: bytes | None = None
        cover_clip: Path | None = None
        for order, (filename, stream) in enumerate(photos):
            is_cover = order + 1 == cover_at
            if Path(filename or "").suffix.lower() in RENDER_CLIP_EXTENSIONS:
                rel, meta = _save_render_clip(job_dir, f"clip_{order}", filename, stream)
                clip_seconds += float(meta["seconds"])
                if clip_seconds > MAX_RENDER_CLIP_TOTAL_SECONDS + 0.05:
                    raise BadRequestException(
                        f"영상은 합쳐서 {MAX_RENDER_CLIP_TOTAL_SECONDS:.0f}초까지 넣을 수 있습니다 "
                        f"(영상마다 앞 {RENDER_CLIP_SECONDS:.0f}초만 쓰입니다)."
                    )
                if is_cover:
                    # 썸네일은 첫 프레임으로 만들고, 인트로는 이 클립 자체를 2초 쓴다.
                    cover_clip = VIDEO_MAKER_DIR / rel
                    cover_source = _first_frame_bytes(cover_clip)
                items.append((rel, meta))
                continue
            # 상한+1바이트까지만 읽어 초과분을 메모리에 올리지 않는다(대표 사진과 같은 방식).
            content = stream.read(MAX_RENDER_PHOTO_BYTES + 1)
            if len(content) > MAX_RENDER_PHOTO_BYTES:
                raise BadRequestException(
                    f"사진 한 장은 {MAX_RENDER_PHOTO_BYTES // (1024 * 1024)}MB 이하만 가능합니다."
                )
            if is_cover:
                cover_source = content  # GPS 판정보다 먼저 — 아래에서 걸러져도 표지로는 쓴다
            meta = _extract_photo_meta(content)
            if meta is None:
                continue  # GPS 없는 사진은 지점을 만들 수 없다 — 저장도 하지 않는다
            items.append((_save_render_image(job_dir, f"photo_{order}", filename, content), meta))

        stops = _group_render_items(items, sort_by_time)
        if len(stops) < 2:
            raise BadRequestException(
                "GPS 정보가 있는 사진·영상이 2개 이상 필요합니다. "
                "(메신저로 전송된 사진·영상은 GPS가 제거되니 원본을 사용하세요)"
            )

        track_points: list[dict[str, object]] = []
        media_points: list[dict[str, object]] = []
        for rels, meta in stops:
            # 순서 지정 모드에서는 촬영 시각이 이동 순서와 어긋날 수 있어 넣지 않는다.
            timestamp = (
                meta["taken"].isoformat() if sort_by_time and meta["taken"] is not None else None
            )
            _append_stop(
                track_points, media_points,
                meta["latitude"], meta["longitude"], rels, timestamp=timestamp,
            )
        _name_stops(track_points, media_points)

        # 지정된 출발지를 맨 앞에 끼워 넣는다 (첫 사진과 사실상 같은 장소면 생략).
        if start_latitude is not None and _haversine_km(
            start_latitude, start_longitude,
            float(track_points[0]["latitude"]), float(track_points[0]["longitude"]),
        ) >= PHOTO_CLUSTER_KM:
            for media in media_points:
                media["trackIndex"] = int(media["trackIndex"]) + 1
            track_points.insert(0, {"latitude": start_latitude, "longitude": start_longitude})
            media_points.insert(
                0, {"trackIndex": 0, "name": (start_name or "출발").strip() or "출발", "photos": []}
            )

        if len(track_points) < 2:
            raise BadRequestException("사진들이 모두 같은 장소라 이동 경로를 만들 수 없습니다.")

        travel_data_path, bgm_path = _write_travel_data(job_dir, track_points, media_points, bgm)
        cover_path = _write_cover(job_dir, cover_source, title)
        return _spawn_render_job(
            db, travel_data_path, bgm_path, theme, user_idx, title,
            region=_region_of_trip(track_points),
            cover_path=cover_path,
            intro_source=_intro_source_path(job_dir, cover_path, cover_clip),
        )
    except Exception:
        # 여기까지 못 오면 렌더 job 이 없어 아무도 이 디렉터리를 치우지 않는다
        # (_run_render_job 의 정리는 job 이 떠야 돈다). 사진이 uploads/ 에 영영 쌓이므로
        # 실패 경로에서 직접 지운다 — 성공하면 렌더 스레드가 끝낼 때 지운다.
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


# --------------------------------------------------------------------------- #
# 여행(travel) 일정으로 렌더링 — 스케줄 좌표·첨부 이미지로 경로를 구성한다.
# --------------------------------------------------------------------------- #
# 여행 렌더 사진 상한. 관광 이미지가 빈 지점을 다 채우면 지점 수만큼 사진이 늘어 60초 상한
# (이동 구간만 압축하고 사진 시간은 안 줄인다)을 넘기므로 여기서 자른다. 15장 = 사진 30초.
# photos-only 렌더에는 안 건다 — 그쪽은 사용자가 장수를 직접 고른 것이라 말없이 빼면 안 된다.
PHOTOS_PER_STOP = 3
MAX_TRAVEL_RENDER_PHOTOS = 15
# 일정에 붙은 사진이 그 일정에서 이만큼 넘게 떨어져 찍혔으면 '코스 밖에서 찍은 사진'으로 보고
# 일정 다음에 경유 지점을 따로 만든다. 지점을 묶는 PHOTO_CLUSTER_KM(1km)보다 넓은 건 큰 명소
# (해운대 해변 1.5km)의 반대편 끝에서 찍은 사진까지 떼어내지 않으려는 것이다.
OFF_COURSE_KM = 2.0


def _split_off_course(schedule, images: list, downloaded: dict[str, bytes | None]):
    """일정에 붙은 사진을 (그 자리에서 찍은 것, 코스 밖에서 찍은 것[(image, meta)])으로 가른다.

    업로드 때 사진은 **거리 상한 없이** 가장 가까운 일정에 붙는다(travel_service._snap_to_schedule).
    그래서 코스에 없는 바다에 다녀온 사진이 15km 떨어진 일정에 붙어 그 일정 이름을 달고 나온다.
    사진 GPS 는 DB 에 없지만 렌더가 이미 받아 둔 바이트의 EXIF 에서 다시 읽을 수 있다.
    GPS 없는 사진·좌표 없는 일정은 판단할 수 없어 그 자리 사진으로 둔다.
    코스 밖 사진은 촬영 시각 순(없으면 업로드 순)이다.
    """
    near, far = [], []
    for image in images:
        content = downloaded.get(image.url)
        meta = _extract_photo_meta(content) if content else None
        if (
            meta is not None and schedule.latitude is not None
            and _haversine_km(float(meta["latitude"]), float(meta["longitude"]),
                              float(schedule.latitude), float(schedule.longitude)) > OFF_COURSE_KM
        ):
            far.append((image, meta))
        else:
            near.append(image)
    far.sort(key=lambda item: (item[1]["taken"] is None, item[1]["taken"] or datetime.min))
    return near, far


def _trim_photos(
    media_points: list[dict[str, object]],
    per_stop: int = PHOTOS_PER_STOP,
    total: int = MAX_TRAVEL_RENDER_PHOTOS,
) -> None:
    """지점당 per_stop 장, 전체 total 장으로 자른다(제자리 수정).

    한 바퀴씩 돈다 — 모든 지점의 1번째, 다음 모든 지점의 2번째… 상한에 닿으면 멈춘다.
    사진 많은 지점 하나가 영상을 독점하지 않고 모든 지점이 최소 한 장은 보이며, 지점 안
    순서(촬영/업로드 순)는 유지된다. 관광 이미지는 지점당 1장이라 첫 바퀴에 다 들어간다.
    """
    keep = [0] * len(media_points)
    budget = total
    for round_ in range(per_stop):
        for i, point in enumerate(media_points):
            if budget and len(point["photos"]) > round_:
                keep[i] += 1
                budget -= 1
    for point, n in zip(media_points, keep):
        del point["photos"][n:]


def _fetch_travel_image(url: str) -> bytes | None:
    """travel_image URL 의 이미지 바이트를 받아온다. 실패하면 None (렌더는 계속).

    우리 버킷 URL 이면 GCS 클라이언트로, 그 외(외부 이미지)는 HTTP 로 받는다.
    """
    try:
        object_path = gcs.object_path_from_url(url)
        if object_path is not None:
            return gcs.download_bytes(object_path)
        # 받으면서 센다 — response.content는 크기 상관없이 통째로 메모리에 올리고, timeout은 읽기 사이
        # 간격이라 총량을 막지 못한다. 홍보 렌더는 image_url을 요청 본문으로 받으므로 수 GB 파일 주소
        # 하나로 서버(e2-small, 2GB)가 죽을 수 있다. 상한은 업로드 사진과 같다(MAX_RENDER_PHOTO_BYTES).
        with requests.get(url, timeout=10, stream=True) as response:
            response.raise_for_status()
            too_big = int(response.headers.get("Content-Length") or 0) > MAX_RENDER_PHOTO_BYTES
            body = bytearray()
            for chunk in [] if too_big else response.iter_content(64 * 1024):
                body += chunk
                if len(body) > MAX_RENDER_PHOTO_BYTES:  # Content-Length가 없거나 거짓이어도 여기서 끊긴다
                    too_big = True
                    break
        if too_big:
            logger.warning("여행 이미지가 %dMB를 넘어 건너뜀: %s", MAX_RENDER_PHOTO_BYTES // (1024 * 1024), url)
            return None
        return bytes(body)
    except Exception:
        logger.warning("여행 이미지 다운로드 실패(건너뜀): %s", url)
        return None


def start_render_travel(
    db: Session,
    user,
    travel_idx: int,
    *,
    bgm: str = "",
    theme: str = "default",
    title: str | None = None,
) -> dict[str, object]:
    """저장된 여행(travel)의 일정·첨부 이미지만으로 여행 경로 영상 렌더링을 시작한다.

    스케줄을 타임라인 순(day_no, sequence)으로 따라가며 지점을 만들고, 각 스케줄에
    첨부된 travel_image 이미지들을 해당 지점에서 보여준다. 직전 지점 기준
    PHOTO_CLUSTER_KM(1km) 미만인 연속 스케줄은 별도 지점 없이 직전 지점에 사진만
    합친다(기차 일정은 출발역 좌표가 경유 지점이 된다). 사진이 없는 일정은 관광 대표
    이미지 1장으로 메운다(추천 코스는 schedule.image_url, 직접 만든 여행은 좌표로 TourAPI
    실시간 조회). 사진은 지점당 PHOTOS_PER_STOP 장·전체 MAX_TRAVEL_RENDER_PHOTOS 장까지
    (_trim_photos). 본인 여행이 아니거나 없으면 404. 이미지 다운로드 실패는 건너뛴다.
    시작과 동시에 요청자(user_idx)의 릴스 행이 "렌더 중" 상태로 만들어지고, 그 reels_idx 로
    진행률을 조회한다.
    """
    theme = _validate_render_options(theme)

    travel = travel_dao.get_by_idx(db, travel_idx)
    if travel is None or travel.user_idx != user.user_idx:
        raise NotFoundException("여행을 찾을 수 없습니다.")

    schedules = schedule_dao.list_by_travel(db, travel_idx)
    if not schedules:
        raise BadRequestException("여행에 일정이 없습니다.")

    # 사진은 여행 단위로 한 번에 읽는다. 일정에 매핑된 사진은 그 일정 좌표에서, 매핑이
    # 안 된 사진(schedule_idx=None)은 아래에서 마지막 지점에 몰아 붙인다.
    images = travel_image_dao.by_travel(db, travel_idx)
    by_schedule: dict[int, list] = {}
    unassigned: list = []
    for image in images:
        if image.schedule_idx is None:
            unassigned.append(image)
        else:
            by_schedule.setdefault(image.schedule_idx, []).append(image)

    # 내 사진이 하나도 안 붙은 일정은 관광 대표 이미지 1장으로 메운다 — 여행 전에 만드는 영상이
    # 지도만 돌지 않게. 추천 코스는 저장할 때 받아둔 schedule.image_url을 그대로 쓰고(API 호출
    # 없음), 직접 만든 여행은 그 값이 늘 비어(장소 검색이 카카오라) 좌표로 실시간 조회한다
    # (일정당 1콜, 실패는 None). 기차 일정은 출발역 좌표라 뺀다 — 역 앞 관광지 사진이 붙으면
    # 안 된다. 내 사진이 있는 일정엔 안 붙는다 — 빈자리용이지 내 사진 옆에 끼우는 게 아니다.
    empty = [s for s in schedules if not by_schedule.get(s.schedule_idx) and s.kind != "train"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        fallback = dict(zip(
            [s.schedule_idx for s in empty],
            pool.map(lambda s: s.image_url or tour_place.image_near(s.latitude, s.longitude), empty),
        ))

    # 이미지 다운로드는 서로 독립이라 병렬로 받는다. 결과를 URL로 찾아 쓰므로 다운로드 순서와
    # 아래 소비 순서가 달라져도 사진이 엉뚱한 지점에 붙지 않고, 같은 URL은 한 번만 받는다.
    urls = list(dict.fromkeys([img.url for img in images] + [u for u in fallback.values() if u]))
    with ThreadPoolExecutor(max_workers=8) as pool:
        downloaded = dict(zip(urls, pool.map(_fetch_travel_image, urls)))

    job_dir = _new_job_dir()
    track_points: list[dict[str, object]] = []
    media_points: list[dict[str, object]] = []
    saved_count = 0
    # 사진별 장소명. 1km 안의 일정은 한 지점으로 묶여 지점 이름이 첫 일정 것 하나뿐이라,
    # 그대로 두면 "경복궁 → 국립민속박물관" 코스에서 박물관 사진 위에 "경복궁"이 뜬다.
    photo_labels: dict[str, str] = {}

    def _save(urls_: list[str]) -> list[str]:
        """다운로드한 바이트를 job 디렉터리에 떨궈 렌더가 읽을 경로 목록으로. 실패분은 건너뛴다."""
        nonlocal saved_count
        saved: list[str] = []
        for url in urls_:
            content = downloaded.get(url)
            if content is None:
                continue
            saved.append(_save_render_image(job_dir, f"img_{saved_count}", url, content))
            saved_count += 1
        return saved

    # 일정마다 붙은 사진을 그 자리에서 찍은 것 / 코스 밖에서 찍은 것으로 가른다. 기차 일정은
    # 빼고 전부 그 자리 사진으로 둔다 — 달리는 기차 안에서 찍어 출발역에서 먼 게 정상이라,
    # 떼어내면 "천안시 ○○동" 같은 경유지가 생기고 기차 일정은 '안 간 곳'으로 빠진다.
    splits = [
        (s, *(_split_off_course(s, by_schedule.get(s.schedule_idx, []), downloaded)
              if s.kind != "train" else (by_schedule.get(s.schedule_idx, []), [])))
        for s in schedules
    ]
    # 붙은 사진이 **전부** 다른 곳에서 찍힌 일정은 안 간 것으로 보고 경로에서 뺀다(코스 대신
    # 바다에 간 경우). 한 장이라도 그 자리 사진이 있거나 사진이 아예 없는 일정은 남는다 —
    # 여행 전에 만드는 영상은 전부 사진 없는 일정이라 영향이 없다.
    unvisited = {s.schedule_idx for s, near, far in splits if far and not near}

    def route_size(skip: set[int]) -> int:
        """사진 없이 지점만 쌓아 본 경로 길이 — 일정을 빼도 경로가 남는지 미리 본다."""
        points: list[dict[str, object]] = []
        stops: list[dict[str, object]] = []
        for schedule, _near, far in splits:
            if schedule.schedule_idx not in skip:
                _append_stop(points, stops, schedule.latitude, schedule.longitude, [])
            for _image, meta in far:
                _append_stop(points, stops, float(meta["latitude"]), float(meta["longitude"]), [])
        return len(points)

    if unvisited and route_size(unvisited) < 2:
        unvisited = set()  # 빼고 나면 경로가 안 그려지면 원래 코스를 유지한다

    course_ids: set[int] = set()  # 코스 일정이 들어간 지점(id) — 사진 배분에서 경유지보다 먼저
    for schedule, near, far in splits:
        if schedule.schedule_idx not in unvisited:
            photos = _save([img.url for img in near])
            if not photos and fallback.get(schedule.schedule_idx):
                photos = _save([fallback[schedule.schedule_idx]])
            # 폴백 판정은 일정 단위다 — 직전 지점 1km 안이라 병합되면 관광 이미지가 이웃 일정의
            # 내 사진 뒤에 이어 붙는다. 코스에 있는 장소라 그대로 둔다.
            # (변수 이름을 title 로 두면 릴스 제목 파라미터를 덮어쓴다 — 실제로 그랬다.)
            stop_title = (schedule.title or "").strip()
            if stop_title:  # 제목 없는 일정은 적지 않는다 → 지점 이름(좌표로 채운 이름)을 쓴다
                photo_labels.update(dict.fromkeys(photos, stop_title))
            _append_stop(
                track_points, media_points,
                schedule.latitude, schedule.longitude, photos, name=schedule.title,
            )
            course_ids.add(id(media_points[-1]))
        # 코스 밖에서 찍은 사진은 그 일정 바로 다음에 경유 지점으로 — 지도가 실제로 간 곳으로
        # 날아가고, 이름은 비워 두어 _name_stops 가 사진 위치로 채운다(일정 이름을 달면 안 된다).
        # ponytail: 위치는 '가장 가까운 일정 다음'이다. 촬영 시각으로 일정 사이 순서를 정하려면
        # 일정마다 시각이 있어야 하는데 직접 만든 여행은 비어 있을 수 있다.
        for image, meta in far:
            _append_stop(
                track_points, media_points,
                float(meta["latitude"]), float(meta["longitude"]), _save([image.url]),
            )

    if len(track_points) < 2:
        raise BadRequestException("일정 지점이 2개 이상이어야 이동 경로를 만들 수 있습니다.")

    # 일정에 매핑되지 않은 사진(업로드 때 EXIF GPS가 없어 붙일 일정을 못 정했거나, 붙어
    # 있던 일정이 삭제된 사진)은 마지막 지점 뒤에 이어 붙인다 — 지도 위 어디에 둘지 알
    # 방법이 없어서다. ponytail: 좌표를 아는 사진만 제 위치에 뜨고 나머지는 끝에 몰린다.
    loose = _save([img.url for img in unassigned])
    media_points[-1]["photos"].extend(loose)
    # 어디서 찍었는지 모르는 사진이라 마지막 일정 이름을 달면 틀린 정보다 — 라벨을 숨긴다.
    photo_labels.update(dict.fromkeys(loose, ""))

    # 사진 상한은 코스 일정부터 채우고 경유지는 남는 몫만 받는다 — 경유지가 많으면 앞쪽
    # 경유지가 몫을 다 가져가 뒤쪽 코스 일정 사진이 잘렸다.
    course = [m for m in media_points if id(m) in course_ids]
    _trim_photos(course)
    _trim_photos(
        [m for m in media_points if id(m) not in course_ids],
        total=MAX_TRAVEL_RENDER_PHOTOS - sum(len(m["photos"]) for m in course),
    )
    # 사진이 다 잘린 경유지는 들를 이유가 없다(사진 때문에 생긴 지점) — 경로에서 뺀다.
    keep = [m for m in media_points if id(m) in course_ids or m["photos"]]
    if 2 <= len(keep) < len(media_points):
        track_points = [track_points[int(m["trackIndex"])] for m in keep]
        media_points = keep
        for index, media in enumerate(media_points):
            media["trackIndex"] = index
    # 이름 조회는 경로가 확정된 뒤에 한다 — 빠질 경유지까지 카카오를 부르지 않게.
    _name_stops(track_points, media_points)

    travel_data_path, bgm_path = _write_travel_data(
        job_dir, track_points, media_points, bgm, photo_labels
    )
    # 제목을 안 주면 여행 이름을 그대로 쓴다 — 이미 조회한 값이라 공짜다.
    # 지역도 마찬가지로 여행에 적힌 값을 먼저 쓰고, 없을 때만 좌표로 역지오코딩한다.
    cover_title = _clean_title(title) or travel.title
    cover_path = _write_cover(job_dir, _pick_travel_cover(images, fallback, downloaded), cover_title)
    return _spawn_render_job(
        db, travel_data_path, bgm_path, theme, user.user_idx,
        cover_title,
        region=(travel.region or "").strip() or _region_of_trip(track_points),
        cover_path=cover_path,
        intro_source=_intro_source_path(job_dir, cover_path),
    )


def _pick_travel_cover(images: list, fallback: dict, downloaded: dict) -> bytes | None:
    """여행 렌더의 표지로 쓸 사진 하나를 **무작위로** 고른다. 쓸 게 없으면 None.

    이 경로엔 사용자가 파일을 고르는 화면이 없어(travel_idx 하나만 보낸다) 번호를 받을
    입구가 없다. 그래서 서버가 고르는데, 앞에서부터 집으면 늘 첫날 첫 일정 사진이라
    같은 여행을 다시 렌더해도 표지가 똑같다 — 무작위면 다시 눌러 다른 표지를 받을 수 있다.

    **내가 올린 사진을 먼저 본다.** 사진이 하나도 없는 일정은 관광 대표 이미지(fallback)로
    메워지는데, 그건 남이 찍은 홍보 사진이라 표지로는 내 사진이 낫다.
    """
    mine = [downloaded.get(image.url) for image in images]
    pool = [content for content in mine if content] or [
        downloaded.get(url) for url in fallback.values() if url and downloaded.get(url)
    ]
    return random.choice(pool) if pool else None


# --------------------------------------------------------------------------- #
# 홍보 영상 — 지자체가 코스(지점 목록)만 등록하면 30초 영상을 만든다.
# --------------------------------------------------------------------------- #
# 본편 상한(초). 인트로·아웃트로(각 ~3.6초)는 이 밖에 붙어 완성본은 37초쯤 된다.
# 상한은 이동 구간만 압축하므로 지점 수가 실제 길이를 정한다 — 지점당 사진 1장 2.8초 +
# 이동 최소 1.2초라 6지점이면 24초쯤. 지점 수 상한(6)은 스키마(PromoRenderRequest)가 건다.
PROMO_VIDEO_SECONDS = 30.0


def start_render_promo(db: Session, user_idx: int, req: PromoRenderRequest) -> dict[str, object]:
    """코스 지점 목록만으로 홍보 영상 렌더링을 시작한다 — 사진 업로드 없음.

    지점마다 image_url 이 있으면 그걸, 없으면 좌표로 관광 대표 이미지를 실시간 조회해
    1장씩 붙인다(둘 다 없거나 다운로드 실패면 사진 없이 지나감). 결과는 요청자 소유의
    보통 릴스 1건이라 공유 링크(/r/{reels_idx})·다운로드·피드 노출이 모두 그대로 된다.
    ponytail: 코스는 저장하지 않는다 — 다시 뽑고 싶으면 다시 부른다. 권한 게이트도 없다.

    cover_index 는 표지로 쓸 **지점 번호(1부터)** 다(안 주면 1번). 사진 업로드가 없는
    경로라 사용자가 고를 수 있는 단위가 지점뿐이다 — 그 지점의 이미지가 없으면
    표지도 없고 완성 영상에서 프레임을 뽑는다.
    """
    theme = _validate_render_options(req.theme)
    cover_at = _resolve_cover_index(req.cover_index, len(req.points))

    with ThreadPoolExecutor(max_workers=8) as pool:
        urls = list(pool.map(
            lambda p: p.image_url or tour_place.image_near(p.latitude, p.longitude), req.points,
        ))
        distinct = list(dict.fromkeys(u for u in urls if u))
        downloaded = dict(zip(distinct, pool.map(_fetch_travel_image, distinct)))

    job_dir = _new_job_dir()
    try:
        track_points: list[dict[str, object]] = []
        media_points: list[dict[str, object]] = []
        cover_source: bytes | None = None
        for i, (point, url) in enumerate(zip(req.points, urls)):
            content = downloaded.get(url) if url else None
            if i + 1 == cover_at:
                cover_source = content
            photos = [_save_render_image(job_dir, f"img_{i}", url, content)] if content else []
            _append_stop(
                track_points, media_points,
                point.latitude, point.longitude, photos, name=point.name.strip(),
            )
        if len(track_points) < 2:
            raise BadRequestException("지점들이 모두 같은 장소라 이동 경로를 만들 수 없습니다.")

        travel_data_path, bgm_path = _write_travel_data(job_dir, track_points, media_points, req.bgm)
        cover_path = _write_cover(job_dir, cover_source, req.title)
        return _spawn_render_job(
            db, travel_data_path, bgm_path, theme, user_idx, req.title,
            region=(req.region or "").strip() or _region_of_trip(track_points),
            max_video_seconds=PROMO_VIDEO_SECONDS,
            cover_path=cover_path,
            intro_source=_intro_source_path(job_dir, cover_path),
        )
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)  # 렌더 job 이 안 떴으면 아무도 안 치운다
        raise


def get_render_job(db: Session, reels_idx: int, user_idx: int) -> dict[str, object]:
    """본인 릴스의 렌더 상태를 반환한다 (진행률·경과·예상 남은 시간 포함).

    인메모리 레지스트리를 먼저 본다 — 렌더가 실패하면 자리표 릴스 행이 삭제되므로
    행부터 찾으면 실패 사유 대신 404 가 나가버린다. 레지스트리에 없으면(서버 재시작
    등) 릴스 행만 보고 만든 상태를 돌려준다. 본인 릴스가 아니거나 없으면 404.
    """
    with _jobs_lock:
        job = _jobs.get(reels_idx)
    if job is not None and job["user_idx"] == user_idx:
        return _job_snapshot(job)
    return _status_from_reels(_load_own_reels(db, reels_idx, user_idx))


def _status_from_reels(reels) -> dict[str, object]:
    """인메모리 진행 정보가 없을 때 릴스 행만으로 만든 상태 응답.

    url 이 채워져 있으면 렌더가 끝난 릴스이고, 비어 있으면 렌더 중이던 작업의
    진행 정보를 서버 재시작으로 잃은 것이다(status=unknown).
    """
    done = bool(reels.url)
    return {
        "reels_idx": reels.reels_idx,
        "status": "done" if done else "unknown",
        "phase": "완료" if done else "진행 상태 없음",
        "percent": 100.0 if done else 0.0,
        "frame": 0,
        "total_frames": None,
        "elapsed_seconds": 0.0,
        "eta_seconds": 0.0 if done else None,
        "engine": _engine(),
        "theme": "",
        "bgm": None,
        "video_url": reels.url or None,
        "reels_url": reels.url or None,
        "error": None if done else "렌더 진행 상태를 알 수 없습니다 (서버가 재시작되었을 수 있습니다).",
    }


def _job_snapshot(job: dict) -> dict[str, object]:
    with _jobs_lock:
        snapshot = dict(job)
    if snapshot["status"] == "running":
        snapshot["elapsed_seconds"] = round(time.time() - snapshot["started_at"], 1)
        # 초반(5% 미만)은 표본이 적어 ETA 가 크게 튀므로 생략.
        if snapshot["percent"] >= 5:
            remaining = snapshot["elapsed_seconds"] * (100 - snapshot["percent"]) / snapshot["percent"]
            snapshot["eta_seconds"] = round(remaining, 1)
    for key in _INTERNAL_JOB_KEYS:
        snapshot.pop(key, None)
    return snapshot


def _run_render_job(job: dict, command: list[str]) -> None:
    """렌더를 돌리고, 끝나면 입력 디렉터리·미완성 릴스를 정리한다 (스레드 진입점).

    렌더 슬롯(_render_slots)이 다 차 있으면 여기서 기다린다. 정리(디렉터리 삭제·
    릴스 정리)는 슬롯을 놓은 뒤에 하므로 GPU 자리를 붙잡지 않는다.

    성공·실패·예외 어느 쪽이든 uploads/<job> 를 지운다. 안 지우면 업로드된
    원본 사진이 계속 쌓여 디스크가 찬다(렌더 산출물과 같은 이유). 끝내 done 이
    되지 못했으면 시작할 때 만들어 둔 릴스 행도 소프트 삭제한다 — 영상 없는
    자리표 행이 DB 에 남지 않게.
    """
    try:
        with _jobs_lock:
            job["phase"] = "대기 중"
        with _render_slots:
            with _jobs_lock:
                # 대기 시간을 그대로 두면 ETA(경과 × 남은 비율)가 통째로 부풀어
                # "5% 인데 1시간 남음"이 나간다. 렌더 시작 시각을 여기서 다시 잡아
                # elapsed_seconds·eta_seconds 가 순수 렌더 시간이 되게 한다.
                job.update(started_at=time.time(), phase="렌더 준비 중")
            _render_job(job, command)
    except Exception:  # 예상 못 한 예외도 job 상태에 남긴다 (사유는 로그에만)
        logger.exception("렌더 작업 처리 중 오류 (reels_idx=%s)", job.get("reels_idx"))
        with _jobs_lock:
            job.update(status="failed", error=FAILED_MESSAGE)
    finally:
        job_dir = job.get("job_dir")
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        if job.get("status") != "done":
            _discard_pending_reels(job["reels_idx"])
        with _jobs_lock:
            _trim_jobs_locked()


def _trim_jobs_locked() -> None:
    """레지스트리를 _MAX_JOBS 이하로 줄인다 — 끝난 job 을 오래된 순으로만 덜어낸다.

    돌고 있는 job 은 절대 안 지운다(폴링 중인 클라이언트가 상태를 잃는다). 그래서 동시
    진행 job 이 상한을 넘으면 그만큼은 그대로 남는다 — 상한은 목표지 강제가 아니다.
    호출자가 _jobs_lock 을 쥔 상태여야 한다.
    """
    excess = len(_jobs) - _MAX_JOBS
    if excess <= 0:
        return
    finished = sorted(
        (key for key, job in _jobs.items() if job["status"] != "running"),
        key=lambda key: _jobs[key]["started_at"],
    )
    for key in finished[:excess]:
        del _jobs[key]


def _render_job(job: dict, command: list[str]) -> None:
    """렌더 서브프로세스를 돌리며 stdout 마커로 job 진행률을 갱신한다."""

    def update(**fields) -> None:
        with _jobs_lock:
            job.update(fields)

    # PYTHONIOENCODING: Windows 파이프 기본 cp949 로 한국어 마커가 깨지지 않게.
    # PYTHONUNBUFFERED: 자식 print 가 파이프에서 버퍼링되지 않고 실시간 스트리밍되게.
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    try:
        process = subprocess.Popen(
            command,
            cwd=str(VIDEO_MAKER_DIR),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        # 보통 설정 문제(= [videomaker] python 경로)다 — 경로가 담긴 메시지는 로그에만.
        logger.exception("렌더 프로세스 시작 실패 (reels_idx=%s)", job["reels_idx"])
        update(status="failed", error=FAILED_MESSAGE)
        return

    # 30분 하드 캡 — 넘으면 프로세스를 죽인다 (아래 read 루프가 EOF 로 끝남).
    timeout_fired = threading.Event()

    def _kill_on_timeout() -> None:
        timeout_fired.set()
        process.kill()

    watchdog = threading.Timer(RENDER_TIMEOUT_SECONDS, _kill_on_timeout)
    watchdog.daemon = True
    watchdog.start()

    lines: list[str] = []
    try:
        for line in process.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            if len(lines) > 5000:
                del lines[:1000]

            match = _FRAME_PROGRESS_RE.search(line)
            if match:
                frame, total = int(match.group(1)), int(match.group(2))
                # 프레임 렌더 구간을 5% → 90% 에 매핑 (앞뒤는 준비/후처리).
                percent = 5 + (frame / total) * 85 if total else 5
                update(
                    frame=frame,
                    total_frames=total,
                    percent=round(percent, 1),
                    phase="프레임 렌더링",
                )
                continue
            for mark, percent, phase in _POSTPROCESS_MARKS:
                if mark in line:
                    update(percent=percent, phase=phase)
                    break
        returncode = process.wait()
    finally:
        watchdog.cancel()

    stdout = "\n".join(lines)
    elapsed = round(time.time() - job["started_at"], 1)

    if timeout_fired.is_set():
        update(
            status="failed",
            error="렌더링이 제한 시간(30분)을 초과했습니다.",
            elapsed_seconds=elapsed,
        )
        return
    if returncode != 0:
        # 원인(렌더 stdout)은 서버 로그에만 남긴다 — 앱에 그대로 실어 보내면 서버 경로·
        # Modal 내부 로그가 사용자 화면까지 간다.
        logger.error(
            "렌더 서브프로세스 실패 (reels_idx=%s, exit=%s):\n%s",
            job["reels_idx"], returncode, stdout[-2000:],
        )
        update(
            status="failed",
            error=FAILED_MESSAGE,
            elapsed_seconds=elapsed,
        )
        return

    output_name = _parse_output_name(stdout)
    if output_name is None:
        logger.error(
            "렌더 출력 파일 경로를 못 찾음 (reels_idx=%s):\n%s",
            job["reels_idx"], stdout[-2000:],
        )
        update(
            status="failed",
            error=FAILED_MESSAGE,
            elapsed_seconds=elapsed,
        )
        return

    update(percent=99.0, phase="영상 업로드(버킷)")
    video_path = OUTPUT_DIR / output_name
    try:
        _prepend_cover_intro(video_path, job)  # TRAILER 대신 표지 2초 (실패해도 본편은 그대로)
        video_url = _publish_reels_video(job["reels_idx"], video_path, job.get("cover_path"))
        # GCS 에 올라갔으므로 로컬 사본은 지운다. 안 지우면 output/ 이 무한히
        # 쌓여 디스크가 찬다(영상 1편이 수십 MB). 실패 시엔 남겨서 받을 수 있게 둔다.
        video_path.unlink(missing_ok=True)
    except Exception as error:
        # 영상은 만들었지만 내려줄 방법이 없다(URL 없음) → 실패로 처리해서
        # 자리표 릴스 행이 정리되게 한다. mp4 는 서버에 남겨 회수할 수 있게 둔다
        # (회수 경로는 응답이 아니라 로그로 남긴다 — 서버 경로를 앱에 흘리지 않는다).
        logger.exception(
            "렌더 결과 업로드 실패: %s (영상은 서버에 보존: output/%s)", output_name, output_name
        )
        update(
            status="failed",
            error="영상 저장에 실패했습니다. 잠시 후 다시 시도해 주세요.",
            elapsed_seconds=round(time.time() - job["started_at"], 1),
        )
        return

    update(
        status="done",
        phase="완료",
        percent=100.0,
        video_url=video_url,
        reels_url=video_url,
        elapsed_seconds=round(time.time() - job["started_at"], 1),
        eta_seconds=0.0,
    )


def _publish_reels_video(
    reels_idx: int, video_path: Path, cover_path: str | None = None
) -> str:
    """완성 영상을 GCS 버킷(reels/)에 올리고 대기 중인 릴스의 url 을 채운다 → 공개 URL.

    행은 렌더 시작 때 이미 만들어져 있으므로(작성자 매핑도 그 때 끝) 여기서는
    url 만 채운다. 렌더 스레드에서 도므로 요청 세션이 아닌 새 세션을 쓴다.

    갱신은 '아직 삭제되지 않은 행'에만 건다(update_url_if_alive) — 렌더 중인 릴스도
    삭제할 수 있어서, 조회와 커밋 사이에 사용자가 지우면 아무도 볼 수 없는 행에
    방금 올린 객체만 매달리기 때문이다. 갱신하지 못하면 예외로 빠져 업로드한
    영상·썸네일을 버킷에서 되돌린다(정리 실패는 객체 경로와 함께 로그로 남겨
    나중에 손으로 회수할 수 있게 둔다).
    """
    from databases.database import SessionLocal

    url = gcs.upload_file(f"reels/{uuid.uuid4().hex}.mp4", video_path, "video/mp4")
    # 대표 사진으로 만든 표지가 있으면 그걸 쓰고, 없으면 예전처럼 영상에서 프레임을 뽑는다
    # (표지 생성·업로드가 실패해도 썸네일 없는 릴스가 되진 않게).
    thumbnail_url = _publish_cover(cover_path) or _publish_thumbnail(video_path)
    db = SessionLocal()
    try:
        reels = reels_dao.get_by_idx(db, reels_idx)
        if reels is None:
            raise NotFoundException(f"릴스(reels_idx={reels_idx})가 사라졌습니다.")
        user_idx = reels.user_idx
        if not reels_dao.update_url_if_alive(db, reels_idx, url, thumbnail_url):
            raise NotFoundException(f"릴스(reels_idx={reels_idx})가 삭제되었습니다.")
        db.commit()
        # '여행 영상 5개 제작' 스탬프 재판정 — 커밋 뒤에 부른다. 날짜가 아니라 행동으로
        # 켜지는 유일한 스탬프라 자정 배치로는 하루가 늦는다. 예외는 안에서 삼킨다.
        # (옛 익명 릴스는 user_idx가 없어 판정할 대상이 없다.)
        if user_idx is not None:
            from services import stamp_service
            stamp_service.award_after_reels(user_idx)
        return url
    except Exception:
        db.rollback()
        # 행 갱신이 안 됐으니 방금 올린 둘 다 되돌린다(정리 실패는 경로만 로그로).
        _delete_object_quietly(url, "고아 영상")
        _delete_object_quietly(thumbnail_url, "고아 썸네일")
        raise
    finally:
        db.close()


def _discard_pending_reels(reels_idx: int) -> None:
    """렌더가 끝내 완료되지 못했을 때 자리표 릴스 행을 DB 에서 삭제한다.

    영상이 없는 자리표 행은 사용자 컨텐츠가 아니라 reels_idx 를 미리 발급하려고
    만든 행이라 흔적을 남기지 않고 하드 삭제한다(소프트 삭제 불변식의 의도적 예외).
    누군가 렌더 중인 릴스에 댓글·좋아요를 남겨 FK 가 걸리면 지울 수 없으니 그 때만
    소프트 삭제로 물러선다. 이미 url 이 채워졌다면(완료 후 다른 이유로 실패 처리된
    경우) 손대지 않는다. 정리에 실패해도 렌더 결과 보고에는 영향이 없어 로그만 남긴다.
    """
    from databases.database import SessionLocal

    db = SessionLocal()
    try:
        reels = reels_dao.get_by_idx(db, reels_idx)
        if reels is None or reels.url:
            return
        try:
            reels_dao.hard_delete(db, reels)
            db.commit()
            return
        except IntegrityError:  # 댓글·좋아요가 참조 중 → 행을 못 지운다
            db.rollback()
        reels = reels_dao.get_by_idx(db, reels_idx)
        if reels is not None and not reels.url:
            reels_dao.soft_delete(db, reels)
            db.commit()
            logger.info("자리표 릴스 하드 삭제 불가(참조 존재) → 소프트 삭제: reels_idx=%s", reels_idx)
    except Exception:
        db.rollback()
        logger.warning("미완성 릴스 정리 실패(무시): reels_idx=%s", reels_idx)
    finally:
        db.close()
