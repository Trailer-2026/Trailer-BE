# -*- coding: utf-8 -*-
"""여행 경로 3D 영상(videoMaker) 서비스.

빌더 페이지가 보낸 GPS 지점/사진/옵션을 travel_data.json 으로 변환해
services/videoMaker/render_video.py(로컬 GPU) 또는 modal_render.py(Modal T4 GPU)를
서브프로세스로 실행하고, 완성된 mp4 파일명을 돌려준다. DB는 사용하지 않는다.

렌더러는 별도 conda 환경(trailer3d)의 의존성(playwright 등)이 필요하므로,
로컬 엔진이 쓸 파이썬 경로를 properties_dev.ini 의 [videomaker] python 으로
지정한다(없으면 현재 프로세스 파이썬 — 메인 서버를 trailer3d 환경으로 띄운 경우).
Modal CLI 경로도 [videomaker] modal 로 지정 가능(없으면 PATH → 파이썬 옆 Scripts 순).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from config import Config
from core.exceptions.custom import (
    BadRequestException,
    ExternalServiceException,
    NotFoundException,
)

VIDEO_MAKER_DIR = Path(__file__).resolve().parent / "videoMaker"
BGM_DIR = VIDEO_MAKER_DIR / "bgm"
UPLOADS_DIR = VIDEO_MAKER_DIR / "assets" / "uploads"
OUTPUT_DIR = VIDEO_MAKER_DIR / "output"
BUILDER_HTML = VIDEO_MAKER_DIR / "builder.html"
MAP_THEMES_JS = VIDEO_MAKER_DIR / "map_themes.js"
RENDER_SCRIPT = VIDEO_MAKER_DIR / "render_video.py"
MODAL_SCRIPT = VIDEO_MAKER_DIR / "modal_render.py"

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
RENDER_TIMEOUT_SECONDS = 60 * 30  # 30분 하드 캡
# render_video.py --theme 와 map_themes.js THEMES 에 맞춰 유지.
ALLOWED_THEMES = {"default", "spring", "summer", "autumn", "winter"}
# Standard 스타일 시간대 조명 (빈 값 = 테마 기본).
ALLOWED_LIGHT_PRESETS = {"", "dawn", "day", "dusk", "night"}


# --------------------------------------------------------------------------- #
# 빌더 페이지 / 정적 자원
# --------------------------------------------------------------------------- #
def get_builder_html() -> str:
    """builder.html 에 Mapbox 토큰을 주입해 반환한다."""
    if not BUILDER_HTML.exists():
        raise NotFoundException("builder.html이 없습니다.")
    token = Config.read("mapbox", "access_token", default="") or ""
    return BUILDER_HTML.read_text(encoding="utf-8").replace("__MAPBOX_TOKEN__", token.strip())


def get_map_themes_path() -> Path:
    """빌더 미리보기가 렌더러와 공유하는 테마 모듈 경로."""
    if not MAP_THEMES_JS.is_file():
        raise NotFoundException("map_themes.js가 없습니다.")
    return MAP_THEMES_JS


def get_output_path(name: str) -> Path:
    """완성 영상 경로를 output/ 밖으로 못 나가게 검증해 반환한다."""
    candidate = (OUTPUT_DIR / name).resolve()
    if candidate.parent != OUTPUT_DIR.resolve() or not candidate.is_file():
        raise NotFoundException("영상을 찾을 수 없습니다.")
    return candidate


# --------------------------------------------------------------------------- #
# BGM
# --------------------------------------------------------------------------- #
def _bgm_display_name(filename: str) -> dict[str, str]:
    """(Pixabay attribution 형식일 수 있는) 파일명을 곡명/아티스트로 정리한다."""
    stem = Path(filename).stem
    # Pixabay 내보내기 파일명 형태:
    #   "Music by a href=...content=469216Denys Kyshchuka from a href=..."
    match = re.search(r"content=\d+(.+?)\s+from\b", stem)
    if match:
        artist = match.group(1).strip(" -_")
        return {"title": "Pixabay BGM", "artist": artist or "Unknown", "source": "Pixabay"}
    return {"title": stem, "artist": "", "source": ""}


def list_bgm() -> list[dict[str, str]]:
    if not BGM_DIR.exists():
        return []
    tracks: list[dict[str, str]] = []
    for path in sorted(BGM_DIR.iterdir()):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
            tracks.append({"file": path.name, **_bgm_display_name(path.name)})
    return tracks


def get_bgm_path(filename: str) -> Path:
    """BGM 파일명을 bgm/ 밖으로 못 나가게 검증해 경로로 반환한다."""
    candidate = (BGM_DIR / filename).resolve()
    if candidate.parent != BGM_DIR.resolve() or not candidate.is_file():
        raise NotFoundException("BGM을 찾을 수 없습니다.")
    if candidate.suffix.lower() not in AUDIO_EXTENSIONS:
        raise BadRequestException("오디오 파일이 아닙니다.")
    return candidate


# --------------------------------------------------------------------------- #
# 렌더링
# --------------------------------------------------------------------------- #
def _render_python() -> str:
    """로컬 엔진(render_video.py)을 실행할 파이썬 경로."""
    return Config.read("videomaker", "python", default=sys.executable) or sys.executable


def _modal_executable() -> str:
    """Modal CLI 경로: 설정 → PATH → 렌더 파이썬 옆 Scripts 순으로 찾는다."""
    configured = Config.read("videomaker", "modal")
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("modal")
    if found:
        return found
    sibling = Path(_render_python()).parent / "Scripts" / "modal.exe"
    if sibling.is_file():
        return str(sibling)
    raise ExternalServiceException(
        "modal CLI를 찾을 수 없습니다. [videomaker] modal 설정 또는 trailer3d 환경 설치가 필요합니다."
    )


def _build_travel_data(
    points_json: str,
    photo_points_json: str,
    photos: list[tuple[str, bytes]],
    bgm: str,
) -> tuple[Path, Path | None]:
    """빌더 입력을 job 디렉터리의 travel_data.json 으로 저장한다.

    반환: (travel_data.json 경로, BGM 경로 또는 None)
    """
    try:
        raw_points = json.loads(points_json)
        photo_owner_indices = json.loads(photo_points_json)
    except json.JSONDecodeError as error:
        raise BadRequestException(f"잘못된 JSON입니다: {error}") from error

    if not isinstance(raw_points, list) or len(raw_points) < 2:
        raise BadRequestException("GPS 지점은 최소 2개가 필요합니다.")
    if len(photo_owner_indices) != len(photos):
        raise BadRequestException("photo_points와 photos 개수가 다릅니다.")

    job_dir = UPLOADS_DIR / uuid.uuid4().hex[:12]
    job_dir.mkdir(parents=True, exist_ok=True)

    # 업로드 사진을 지점별로 저장.
    photos_by_point: dict[int, list[str]] = {}
    for order, ((filename, content), owner) in enumerate(zip(photos, photo_owner_indices)):
        try:
            point_index = int(owner)
        except (TypeError, ValueError):
            continue
        suffix = Path(filename or "").suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            suffix = ".jpg"
        saved = job_dir / f"p{point_index}_{order}{suffix}"
        saved.write_bytes(content)
        rel = saved.relative_to(VIDEO_MAKER_DIR).as_posix()
        photos_by_point.setdefault(point_index, []).append(rel)

    track_points: list[dict[str, object]] = []
    media_points: list[dict[str, object]] = []
    for index, point in enumerate(raw_points):
        try:
            latitude = float(point["latitude"])
            longitude = float(point["longitude"])
        except (KeyError, TypeError, ValueError) as error:
            raise BadRequestException(f"지점 {index}의 좌표가 올바르지 않습니다.") from error
        track_points.append({"latitude": latitude, "longitude": longitude})
        name = str(point.get("name") or f"지점 {index + 1}").strip()
        media_points.append(
            {"trackIndex": index, "name": name, "photos": photos_by_point.get(index, [])}
        )

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


def _build_command(
    travel_data_path: Path,
    engine: str,
    quick: bool,
    theme: str,
    light_preset: str,
    intro: bool,
    outro: bool,
) -> tuple[list[str], str]:
    """엔진별 렌더 명령을 만든다. 반환: (command, 출력 파일명 파싱용 marker)"""
    if engine == "modal":
        command = [
            _modal_executable(),
            "run",
            str(MODAL_SCRIPT),
            "--mode",
            "quality-fast" if quick else "quality",
            "--travel-data",
            travel_data_path.relative_to(VIDEO_MAKER_DIR).as_posix(),
        ]
    else:
        command = [
            _render_python(),
            str(RENDER_SCRIPT),
            "--travel-data",
            str(travel_data_path),
        ]
        if quick:
            command.append("--quick")
    if theme != "default":
        command += ["--theme", theme]
    if light_preset:
        command += ["--light-preset", light_preset]
    if intro:
        command.append("--intro")
    if outro:
        command.append("--outro")
    return command, engine


def _parse_output_name(stdout: str, marker: str) -> str | None:
    """렌더 서브프로세스 stdout 에서 완성 파일명을 뽑는다.

    local -> render_video.py 가 "출력 예정 파일: <path>" 를 출력.
    modal -> modal_render.py 엔트리포인트가 "저장 위치: <path>" 를 출력
             (컨테이너의 "출력 예정 파일" 은 /app 경로라 로컬 파일이 아님).
    """
    pattern = r"저장 위치:\s*(.+)" if marker == "modal" else r"출력 예정 파일:\s*(.+)"
    match = re.search(pattern, stdout)
    if match:
        return Path(match.group(1).strip()).name
    # 폴백: output/ 의 가장 최근 mp4.
    if OUTPUT_DIR.exists():
        mp4s = sorted(OUTPUT_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        if mp4s:
            return mp4s[-1].name
    return None


def render(
    points_json: str,
    photo_points_json: str,
    photos: list[tuple[str, bytes]],
    bgm: str = "",
    quick: bool = False,
    engine: str = "local",
    theme: str = "default",
    light_preset: str = "",
    intro: bool = False,
    outro: bool = False,
) -> dict[str, object]:
    """영상을 렌더링하고 결과 메타데이터(dict)를 반환한다. 완료까지 블로킹."""
    engine = (engine or "local").lower().strip()
    if engine not in {"local", "modal"}:
        raise BadRequestException(f"알 수 없는 엔진: {engine}")
    theme = (theme or "default").lower().strip()
    if theme not in ALLOWED_THEMES:
        raise BadRequestException(f"알 수 없는 테마: {theme}")
    light_preset = (light_preset or "").lower().strip()
    if light_preset not in ALLOWED_LIGHT_PRESETS:
        raise BadRequestException(f"알 수 없는 조명: {light_preset}")

    travel_data_path, bgm_path = _build_travel_data(points_json, photo_points_json, photos, bgm)
    command, marker = _build_command(
        travel_data_path, engine, quick, theme, light_preset, intro, outro
    )

    render_started = time.perf_counter()
    # Windows 파이프 기본 인코딩(cp949)과 어긋나지 않게 자식 stdout 을 utf-8 로 고정
    # ("출력 예정 파일:" 등 한국어 마커 파싱이 깨지지 않도록).
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        result = subprocess.run(
            command,
            cwd=str(VIDEO_MAKER_DIR),
            env=child_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RENDER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise ExternalServiceException("렌더링이 제한 시간(30분)을 초과했습니다.") from error
    elapsed_seconds = round(time.perf_counter() - render_started, 1)

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if result.returncode != 0:
        tail = (stdout + "\n" + stderr)[-2000:]
        raise ExternalServiceException(f"렌더링 실패:\n{tail}")

    output_name = _parse_output_name(stdout, marker)
    if output_name is None:
        raise ExternalServiceException("출력 파일 경로를 확인할 수 없습니다.")

    return {
        "video_url": f"/api/videos/output/{output_name}",
        "engine": engine,
        "theme": theme,
        "light_preset": light_preset or None,
        "intro": intro,
        "outro": outro,
        "bgm": bgm_path.name if bgm_path else None,
        "elapsed_seconds": elapsed_seconds,
        "log_tail": stdout[-2000:],
    }
