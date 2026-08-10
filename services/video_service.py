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
    ReelsTitleUpdateResponse,
    ReelsUploadResponse,
)
from utils import gcs, kakao_local

logger = logging.getLogger(__name__)


VIDEO_MAKER_DIR = Path(__file__).resolve().parent / "videoMaker"
BGM_DIR = VIDEO_MAKER_DIR / "bgm"
UPLOADS_DIR = VIDEO_MAKER_DIR / "assets" / "uploads"
OUTPUT_DIR = VIDEO_MAKER_DIR / "output"
MAP_THEMES_JS = VIDEO_MAKER_DIR / "map_themes.js"
# 배포된 Modal 함수를 호출하는 러너 (사전 1회: modal deploy modal_render.py)
MODAL_CALL_SCRIPT = VIDEO_MAKER_DIR / "modal_call.py"

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
RENDER_TIMEOUT_SECONDS = 60 * 30  # 30분 하드 캡
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
    로그인 상태면 내가 차단한 사용자의 릴스는 두 경로 모두에서 빠진다(비로그인은 user=None).
    """
    exclude_idxs: list[int] = []
    for token in exclude.split(","):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit():
            raise BadRequestException("exclude는 쉼표로 구분한 reels_idx 목록이어야 합니다.")
        exclude_idxs.append(int(token))

    blocked = ban_dao.blocked_user_idxs(db, user.user_idx) if user else []
    rows = reels_dao.get_random_reels(db, count, exclude_idxs, blocked)
    if not rows and exclude_idxs:
        # 전부 이미 추천된 상태 → 한 바퀴 돌았으니 처음부터 다시
        rows = reels_dao.get_random_reels(db, count, [], blocked)
    # 좋아요·댓글 수는 행마다 세면 N+1 이라 뽑힌 릴스만 묶어 두 번에 읽는다.
    idxs = [reels.reels_idx for reels, _, _ in rows]
    like_counts = like_dao.counts_by_reels(db, idxs)
    comment_counts = comment_dao.counts_by_reels(db, idxs)
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
    """
    rows = reels_dao.list_by_user(db, user.user_idx, limit + 1, cursor)
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = _to_reels_cards(
        db, user, [(reels, user.nickname, user.profile_image) for reels in rows]
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
        db, user, [(reels, nickname, profile_image) for _, reels, nickname, profile_image in rows]
    )
    return MyReelsListResponse(
        items=items,
        next_cursor=rows[-1][0] if has_more and rows else None,
    )


def _to_reels_cards(db: Session, user, rows) -> list[MyReelsItem]:
    """(릴스, 닉네임, 프로필) 목록에 좋아요·댓글 수와 내 좋아요 여부를 붙인다.

    recommend_reels 와 같은 이유로 카운트는 행마다 세지 않고 뽑힌 릴스만 묶어
    일괄 조회한다(N+1 회피).
    """
    idxs = [reels.reels_idx for reels, _, _ in rows]
    like_counts = like_dao.counts_by_reels(db, idxs)
    comment_counts = comment_dao.counts_by_reels(db, idxs)
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


def _build_command(travel_data_path: Path, theme: str) -> list[str]:
    """Modal 렌더 명령을 만든다.

    화질은 항상 quality-fast(JPEG q95, 풀해상도) — 무손실 PNG 대비 최종 mp4
    화질 차이가 사실상 없고 렌더가 크게 빠르다(modal_call.py 의 기본 --mode).
    """
    command = [
        _render_python(),
        str(MODAL_CALL_SCRIPT),
        "--travel-data",
        travel_data_path.relative_to(VIDEO_MAKER_DIR).as_posix(),
    ]
    if theme != "default":
        command += ["--theme", theme]
    # TRAILER 인트로·아웃트로는 항상 붙인다.
    command += ["--intro", "--outro"]
    return command


def _parse_output_name(stdout: str) -> str | None:
    """렌더 서브프로세스 stdout 에서 완성 파일명을 뽑는다.

    modal_call.py 가 조각을 합친 뒤 "저장 위치: <path>" 를 출력한다
    (컨테이너가 찍는 "출력 예정 파일" 은 /app 경로라 로컬 파일이 아니다).
    """
    match = re.search(r"저장 위치:\s*(.+)", stdout)
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
# photo_fade_in(0.4) + photo_hold(1.6) + photo_fade_out(0.4). 그쪽이 바뀌면 같이 고칠 것.
INSERT_PHOTO_SECONDS = 2.4
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
# 업로드 상한. 디스크·버킷을 지키는 선이라 넉넉하되 무제한은 아니다.
MAX_UPLOAD_BYTES = 300 * 1024 * 1024


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
        except ExternalServiceException as error:  # ffprobe 가 있는데 못 읽음 = 파일 문제
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

# 렌더가 끝나기 전 릴스 행의 url 자리표. url 은 NOT NULL 이라 NULL 을 못 쓴다.
# 이 값인 행은 "렌더 중"이라 추천 피드에서 빠지고(reels_dao.get_random_reels)
# 다운로드·편집도 거부한다(_require_ready).
PENDING_REELS_URL = ""

# job dict 에만 두고 상태 응답(_job_snapshot)에서는 빼는 내부 필드.
_INTERNAL_JOB_KEYS = ("started_at", "user_idx", "job_dir")

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


def _spawn_render_job(
    db: Session,
    travel_data_path: Path,
    bgm_path: Path | None,
    theme: str,
    user_idx: int,
    title: str | None = None,
    region: str | None = None,
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

    command = _build_command(travel_data_path, theme)
    job = {
        "user_idx": user_idx,
        # 렌더 입력(업로드 사진·travel_data.json)이 담긴 디렉터리 — 끝나면 지운다.
        "job_dir": str(travel_data_path.parent),
        "reels_idx": reels.reels_idx,
        "reels_url": None,
        "status": "running",
        "phase": "렌더 준비 중",
        "percent": 0.0,
        "frame": 0,
        "total_frames": None,
        "started_at": time.time(),
        "elapsed_seconds": 0.0,
        "eta_seconds": None,
        "engine": "modal",
        "theme": theme,
        "bgm": bgm_path.name if bgm_path else None,
        "video_url": None,
        "error": None,
        "log_tail": "",
    }
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


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


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

    새 지점의 name 을 생략하면 "지점 N"(추가 후 순번)으로 채운다.
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
        "name": name if name is not None else f"지점 {len(track_points)}",
        "photos": photos,
    })


def _write_travel_data(
    job_dir: Path,
    track_points: list[dict[str, object]],
    media_points: list[dict[str, object]],
    bgm: str,
) -> tuple[Path, Path | None]:
    """travel_data.json 을 job 디렉터리에 쓰고 (경로, BGM 경로|None)을 반환한다."""
    travel_data: dict[str, object] = {
        "trackPoints": track_points,
        "mediaPoints": media_points,
    }
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
    photos: list[tuple[str, bytes]],
    *,
    user_idx: int,
    bgm: str = "",
    theme: str = "default",
    start_name: str = "",
    start_latitude: float | None = None,
    start_longitude: float | None = None,
    sort_by_time: bool = True,
    title: str | None = None,
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
    """
    theme = _validate_render_options(theme)

    # 시작 위치 검증 — 위도/경도는 함께 와야 한다.
    if (start_latitude is None) != (start_longitude is None):
        raise BadRequestException("시작 위치는 위도/경도를 함께 보내야 합니다.")
    if start_latitude is not None and not (
        -90 <= start_latitude <= 90 and -180 <= start_longitude <= 180
    ):
        raise BadRequestException("시작 위치 좌표가 올바르지 않습니다.")
    if len(photos) < 2:
        raise BadRequestException("사진이 2장 이상 필요합니다.")

    tagged: list[tuple[str, bytes, dict]] = []
    for filename, content in photos:
        meta = _extract_photo_meta(content)
        if meta is not None:
            tagged.append((filename, content, meta))
    if len(tagged) < 2:
        raise BadRequestException(
            "GPS 정보가 있는 사진이 2장 이상 필요합니다. "
            "(메신저로 전송된 사진은 GPS가 제거되니 원본을 사용하세요)"
        )

    # 촬영 시각 순 정렬 — 시각 없는 사진은 업로드 순서를 유지한 채 뒤로 보낸다.
    # (순서 지정 모드에서는 업로드 순서가 곧 사용자 지정 순서라 정렬하지 않는다.)
    if sort_by_time:
        tagged.sort(key=lambda item: (item[2]["taken"] is None, item[2]["taken"] or datetime.min))

    job_dir = _new_job_dir()
    track_points: list[dict[str, object]] = []
    media_points: list[dict[str, object]] = []
    for order, (filename, content, meta) in enumerate(tagged):
        rel = _save_render_image(job_dir, f"photo_{order}", filename, content)
        # 순서 지정 모드에서는 촬영 시각이 이동 순서와 어긋날 수 있어 넣지 않는다.
        timestamp = (
            meta["taken"].isoformat() if sort_by_time and meta["taken"] is not None else None
        )
        _append_stop(
            track_points, media_points,
            meta["latitude"], meta["longitude"], [rel], timestamp=timestamp,
        )

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
    return _spawn_render_job(
        db, travel_data_path, bgm_path, theme, user_idx, title,
        region=_region_of_trip(track_points),
    )


# --------------------------------------------------------------------------- #
# 여행(travel) 일정으로 렌더링 — 스케줄 좌표·첨부 이미지로 경로를 구성한다.
# --------------------------------------------------------------------------- #
def _fetch_travel_image(url: str) -> bytes | None:
    """travel_image URL 의 이미지 바이트를 받아온다. 실패하면 None (렌더는 계속).

    우리 버킷 URL 이면 GCS 클라이언트로, 그 외(외부 이미지)는 HTTP 로 받는다.
    """
    try:
        object_path = gcs.object_path_from_url(url)
        if object_path is not None:
            return gcs.download_bytes(object_path)
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.content
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
    합친다(기차 일정은 출발역 좌표가 경유 지점이 된다). 본인 여행이 아니거나 없으면
    404. 이미지 다운로드 실패는 건너뛴다. 시작과 동시에 요청자(user_idx)의 릴스 행이
    "렌더 중" 상태로 만들어지고, 그 reels_idx 로 진행률을 조회한다.
    """
    theme = _validate_render_options(theme)

    travel = travel_dao.get_by_idx(db, travel_idx)
    if travel is None or travel.user_idx != user.user_idx:
        raise NotFoundException("여행을 찾을 수 없습니다.")

    schedules = schedule_dao.list_by_travel(db, travel_idx)
    if not schedules:
        raise BadRequestException("여행에 일정이 없습니다.")

    images_by_schedule = travel_image_dao.urls_by_schedule(
        db, [s.schedule_idx for s in schedules]
    )

    # 이미지 다운로드는 서로 독립이라 병렬로 받는다 (결과 순서는 스케줄 순서 그대로).
    urls = [url for s in schedules for url in images_by_schedule.get(s.schedule_idx, [])]
    with ThreadPoolExecutor(max_workers=8) as pool:
        contents = iter(list(pool.map(_fetch_travel_image, urls)))

    job_dir = _new_job_dir()
    track_points: list[dict[str, object]] = []
    media_points: list[dict[str, object]] = []
    saved_count = 0
    for schedule in schedules:
        photos: list[str] = []
        for url in images_by_schedule.get(schedule.schedule_idx, []):
            content = next(contents)
            if content is None:
                continue
            photos.append(_save_render_image(job_dir, f"img_{saved_count}", url, content))
            saved_count += 1
        _append_stop(
            track_points, media_points,
            schedule.latitude, schedule.longitude, photos, name=schedule.title,
        )

    if len(track_points) < 2:
        raise BadRequestException("일정 지점이 2개 이상이어야 이동 경로를 만들 수 있습니다.")

    travel_data_path, bgm_path = _write_travel_data(job_dir, track_points, media_points, bgm)
    # 제목을 안 주면 여행 이름을 그대로 쓴다 — 이미 조회한 값이라 공짜다.
    # 지역도 마찬가지로 여행에 적힌 값을 먼저 쓰고, 없을 때만 좌표로 역지오코딩한다.
    return _spawn_render_job(
        db, travel_data_path, bgm_path, theme, user.user_idx,
        _clean_title(title) or travel.title,
        region=(travel.region or "").strip() or _region_of_trip(track_points),
    )


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
        "engine": "modal",
        "theme": "",
        "bgm": None,
        "video_url": reels.url or None,
        "reels_url": reels.url or None,
        "error": None if done else "렌더 진행 상태를 알 수 없습니다 (서버가 재시작되었을 수 있습니다).",
        "log_tail": "",
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

    성공·실패·예외 어느 쪽이든 uploads/<job> 를 지운다. 안 지우면 업로드된
    원본 사진이 계속 쌓여 디스크가 찬다(렌더 산출물과 같은 이유). 끝내 done 이
    되지 못했으면 시작할 때 만들어 둔 릴스 행도 소프트 삭제한다 — 영상 없는
    자리표 행이 DB 에 남지 않게.
    """
    try:
        _render_job(job, command)
    except Exception as error:  # 예상 못 한 예외도 job 상태에 남긴다
        logger.exception("렌더 작업 처리 중 오류 (reels_idx=%s)", job.get("reels_idx"))
        with _jobs_lock:
            job.update(status="failed", error=f"렌더 처리 중 오류: {error}")
    finally:
        job_dir = job.get("job_dir")
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        if job.get("status") != "done":
            _discard_pending_reels(job["reels_idx"])


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
    except OSError as error:
        update(status="failed", error=f"렌더 프로세스 시작 실패: {error}")
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
            log_tail=stdout[-2000:],
        )
        return
    if returncode != 0:
        update(
            status="failed",
            error=f"렌더링 실패:\n{stdout[-2000:]}",
            elapsed_seconds=elapsed,
            log_tail=stdout[-2000:],
        )
        return

    output_name = _parse_output_name(stdout)
    if output_name is None:
        update(
            status="failed",
            error="출력 파일 경로를 확인할 수 없습니다.",
            elapsed_seconds=elapsed,
            log_tail=stdout[-2000:],
        )
        return

    log_tail = stdout[-2000:]
    update(percent=99.0, phase="영상 업로드(버킷)")
    video_path = OUTPUT_DIR / output_name
    try:
        video_url = _publish_reels_video(job["reels_idx"], video_path)
        # GCS 에 올라갔으므로 로컬 사본은 지운다. 안 지우면 output/ 이 무한히
        # 쌓여 디스크가 찬다(영상 1편이 수십 MB). 실패 시엔 남겨서 받을 수 있게 둔다.
        video_path.unlink(missing_ok=True)
    except Exception as error:
        # 영상은 만들었지만 내려줄 방법이 없다(URL 없음) → 실패로 처리해서
        # 자리표 릴스 행이 정리되게 한다. mp4 는 서버에 남겨 회수할 수 있게 둔다.
        logger.exception("렌더 결과 업로드 실패: %s", output_name)
        update(
            status="failed",
            error=f"영상 저장에 실패했습니다: {error}",
            elapsed_seconds=round(time.time() - job["started_at"], 1),
            log_tail=log_tail + f"\n[warn] 영상은 서버에 보존: output/{output_name}",
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
        log_tail=log_tail,
    )


def _publish_reels_video(reels_idx: int, video_path: Path) -> str:
    """완성 영상을 GCS 버킷(reels/)에 올리고 대기 중인 릴스의 url 을 채운다 → 공개 URL.

    행은 렌더 시작 때 이미 만들어져 있으므로(작성자 매핑도 그 때 끝) 여기서는
    url 만 채운다. 렌더 스레드에서 도므로 요청 세션이 아닌 새 세션을 쓴다.
    """
    from databases.database import SessionLocal

    url = gcs.upload_file(f"reels/{uuid.uuid4().hex}.mp4", video_path, "video/mp4")
    thumbnail_url = _publish_thumbnail(video_path)  # 홈 카드용 대표 프레임 (실패해도 None)
    db = SessionLocal()
    try:
        reels = reels_dao.get_by_idx(db, reels_idx)
        if reels is None:
            raise NotFoundException(f"릴스(reels_idx={reels_idx})가 사라졌습니다.")
        reels_dao.update_url(db, reels, url, thumbnail_url)
        db.commit()
        return url
    except Exception:
        db.rollback()
        gcs.delete_object(gcs.object_path_from_url(url) or url)  # 고아 객체 정리
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
