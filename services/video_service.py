# -*- coding: utf-8 -*-
"""여행 경로 3D 영상(videoMaker) 서비스.

빌더 페이지가 보낸 GPS 지점/사진/옵션을 travel_data.json 으로 변환해
services/videoMaker/render_video.py(로컬 GPU) 또는 modal_render.py(Modal T4 GPU)를
서브프로세스로 실행하고, 완성된 mp4 파일명을 돌려준다. 렌더 관련 기능은 DB를
쓰지 않으며, 릴스 추천(recommend_reels)만 reels 테이블을 읽는다.

렌더러는 별도 conda 환경(trailer3d)의 의존성(playwright 등)이 필요하므로,
로컬 엔진이 쓸 파이썬 경로를 properties_dev.ini 의 [videomaker] python 으로
지정한다(없으면 현재 프로세스 파이썬 — 메인 서버를 trailer3d 환경으로 띄운 경우).
Modal CLI 경로도 [videomaker] modal 로 지정 가능(없으면 PATH → 파이썬 옆 Scripts 순).
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image
from sqlalchemy.orm import Session

from config import Config
from core.exceptions.custom import (
    BadRequestException,
    ExternalServiceException,
    NotFoundException,
)
from databases.daos import reels_dao
from utils import gcs

logger = logging.getLogger(__name__)

VIDEO_MAKER_DIR = Path(__file__).resolve().parent / "videoMaker"
BGM_DIR = VIDEO_MAKER_DIR / "bgm"
UPLOADS_DIR = VIDEO_MAKER_DIR / "assets" / "uploads"
OUTPUT_DIR = VIDEO_MAKER_DIR / "output"
MAP_THEMES_JS = VIDEO_MAKER_DIR / "map_themes.js"
RENDER_SCRIPT = VIDEO_MAKER_DIR / "render_video.py"
# 배포된 Modal 함수를 호출하는 러너 (사전 1회: modal deploy modal_render.py)
MODAL_CALL_SCRIPT = VIDEO_MAKER_DIR / "modal_call.py"

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
RENDER_TIMEOUT_SECONDS = 60 * 30  # 30분 하드 캡
# render_video.py --theme 와 map_themes.js THEMES 에 맞춰 유지.
ALLOWED_THEMES = {"default", "spring", "summer", "autumn", "winter"}
# Standard 스타일 시간대 조명 (빈 값 = 테마 기본).
ALLOWED_LIGHT_PRESETS = {"", "dawn", "day", "dusk", "night"}


# --------------------------------------------------------------------------- #
# 정적 자원
# --------------------------------------------------------------------------- #
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
def get_bgm_path(filename: str) -> Path:
    """BGM 파일명을 bgm/ 밖으로 못 나가게 검증해 경로로 반환한다."""
    candidate = (BGM_DIR / filename).resolve()
    if candidate.parent != BGM_DIR.resolve() or not candidate.is_file():
        raise NotFoundException("BGM을 찾을 수 없습니다.")
    if candidate.suffix.lower() not in AUDIO_EXTENSIONS:
        raise BadRequestException("오디오 파일이 아닙니다.")
    return candidate


# --------------------------------------------------------------------------- #
# 릴스 추천
# --------------------------------------------------------------------------- #
RECOMMEND_REELS_COUNT = 10


def recommend_reels(db: Session, exclude: str) -> list[dict]:
    """릴스를 무작위로 최대 10개 추천한다.

    exclude(쉼표 구분 reels_idx 목록)에 담긴 릴스는 제외하고 뽑는다 — 프론트가
    이미 받은 idx를 누적해 재요청하면 새 릴스만 내려간다. 남은 릴스가 10개
    미만이면 있는 만큼만 반환하고, 제외 후 남은 릴스가 하나도 없으면 exclude를
    무시하고 전체에서 처음부터 다시 추천한다.
    """
    exclude_idxs: list[int] = []
    for token in exclude.split(","):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit():
            raise BadRequestException("exclude는 쉼표로 구분한 reels_idx 목록이어야 합니다.")
        exclude_idxs.append(int(token))

    rows = reels_dao.get_random_reels(db, RECOMMEND_REELS_COUNT, exclude_idxs)
    if not rows and exclude_idxs:
        # 전부 이미 추천된 상태 → 한 바퀴 돌았으니 처음부터 다시
        rows = reels_dao.get_random_reels(db, RECOMMEND_REELS_COUNT, [])
    return [
        {
            "reels_idx": reels.reels_idx,
            "url": reels.url,
            "title": reels.title,
            "nickname": nickname,
            "profile_image": profile_image,
        }
        for reels, nickname, profile_image in rows
    ]


# --------------------------------------------------------------------------- #
# 렌더링
# --------------------------------------------------------------------------- #
def _render_python() -> str:
    """로컬 엔진(render_video.py)을 실행할 파이썬 경로."""
    return Config.read("videomaker", "python", default=sys.executable) or sys.executable


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
    """엔진별 렌더 명령을 만든다. 반환: (command, 출력 파일명 파싱용 marker)

    화질 기본값은 quality-fast(JPEG q95, 풀해상도) — 무손실 PNG(quality) 대비
    최종 mp4 화질 차이가 사실상 없고 렌더가 크게 빠르다. local 의 quick 만
    저해상도 테스트 모드로 남긴다.
    """
    if engine == "modal":
        # 배포된 함수 원격 호출 (quick 여부와 무관하게 풀해상도 quality-fast).
        command = [
            _render_python(),
            str(MODAL_CALL_SCRIPT),
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
        command.append("--quick" if quick else "--quality-fast")
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


# --------------------------------------------------------------------------- #
# 완성 영상 편집 (ffmpeg 후처리) — 구간 삭제 / 이미지 오버레이
# --------------------------------------------------------------------------- #
EDIT_TIMEOUT_SECONDS = 60 * 5
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


def _edited_output_path(source: Path, tag: str) -> Path:
    """원본 이름 뒤에 _<tag>N 을 붙인, 아직 없는 출력 경로를 반환한다."""
    index = 1
    while True:
        candidate = OUTPUT_DIR / f"{source.stem}_{tag}{index}.mp4"
        if not candidate.exists():
            return candidate
        index += 1


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


def _edit_result(source_started: float, target: Path) -> dict[str, object]:
    info = _ffprobe_video(target)
    return {
        "video_url": f"/api/videos/output/{target.name}",
        "duration_seconds": round(float(info["duration"]), 2),
        "elapsed_seconds": round(time.perf_counter() - source_started, 1),
    }


def cut_video(name: str, start_seconds: float, end_seconds: float) -> dict[str, object]:
    """완성 영상에서 [start, end) 구간을 잘라낸 새 영상을 만든다."""
    source = get_output_path(name)
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

    target = _edited_output_path(source, "cut")
    started = time.perf_counter()
    _run_ffmpeg([
        "-i", str(source),
        "-filter_complex", ";".join(filters),
        *maps, *_EDIT_VIDEO_ARGS, *audio_args,
        str(target),
    ])
    return _edit_result(started, target)


def insert_image(
    name: str,
    at_seconds: float,
    image_filename: str,
    image_bytes: bytes,
    photo_seconds: float = 1.5,
) -> dict[str, object]:
    """완성 영상의 at 시점에 이미지를 전체 화면으로 끼워 넣은 새 영상을 만든다.

    본편 [0, at) 재생 → 사진 photo_seconds 초 → 본편 [at, 끝) 재생.
    렌더러의 사진 구간처럼 본편이 멈추고 사진이 나온 뒤 이어지는 삽입 방식이라
    영상 길이가 photo_seconds 만큼 늘어난다. 사진은 화면을 꽉 채우도록
    비율 유지 확대 후 중앙 크롭한다.
    """
    source = get_output_path(name)
    info = _ffprobe_video(source)
    duration = float(info["duration"])
    at = float(at_seconds)
    photo = float(photo_seconds)
    if at < 0 or at > duration:
        raise BadRequestException(f"삽입 시점은 0 ~ 영상 길이({duration:.1f}초) 사이여야 합니다.")
    if not 0.3 <= photo <= 10:
        raise BadRequestException("사진 표시 시간은 0.3 ~ 10초 사이여야 합니다.")

    suffix = Path(image_filename or "").suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        raise BadRequestException("이미지 파일이 아닙니다 (jpg/png/webp 등).")
    if not image_bytes:
        raise BadRequestException("이미지 파일이 비어 있습니다.")

    width, height = int(info["width"]), int(info["height"])
    fps = str(info["fps"])
    sample_rate = int(info["sample_rate"])
    temp_dir = VIDEO_MAKER_DIR / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    image_path = temp_dir / f"insert_{uuid.uuid4().hex[:8]}{suffix}"
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

    target = _edited_output_path(source, "img")
    started = time.perf_counter()
    try:
        _run_ffmpeg([
            *inputs,
            "-filter_complex", ";".join(filters),
            *maps, *_EDIT_VIDEO_ARGS, *audio_args,
            str(target),
        ])
    finally:
        image_path.unlink(missing_ok=True)
    return _edit_result(started, target)


# --------------------------------------------------------------------------- #
# 렌더 작업(job) 관리 — 진행률 조회를 위해 비동기로 돌린다.
#
# POST /render 는 job_id 를 즉시 반환하고, 렌더 서브프로세스는 데몬 스레드에서
# stdout 을 한 줄씩 읽으며 진행률을 갱신한다. 클라이언트는 GET /render/{job_id}
# 로 폴링한다. 레지스트리는 인메모리라 서버 재시작(--reload 포함) 시 사라진다.
# --------------------------------------------------------------------------- #
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# render_video.py 가 15프레임마다 찍는 "[perf:frame] 000060/000127 ..." 라인.
_FRAME_PROGRESS_RE = re.compile(r"\[perf:frame\]\s*(\d+)/(\d+)")
# 프레임 이후 후처리 마커 → 해당 시점의 percent.
_POSTPROCESS_MARKS = [
    ("렌더링 완료", 92.0, "영상 인코딩 마무리"),
    ("BGM 합성 완료", 95.0, "후처리(BGM)"),
    ("인트로 합성 완료", 97.0, "후처리(인트로)"),
    ("아웃트로 합성 완료", 98.0, "후처리(아웃트로)"),
]


def _validate_render_options(engine: str, theme: str, light_preset: str) -> tuple[str, str, str]:
    """엔진/테마/조명 옵션을 정규화·검증한다 (400 은 여기서 동기적으로 발생)."""
    engine = (engine or "local").lower().strip()
    if engine not in {"local", "modal"}:
        raise BadRequestException(f"알 수 없는 엔진: {engine}")
    theme = (theme or "default").lower().strip()
    if theme not in ALLOWED_THEMES:
        raise BadRequestException(f"알 수 없는 테마: {theme}")
    light_preset = (light_preset or "").lower().strip()
    if light_preset not in ALLOWED_LIGHT_PRESETS:
        raise BadRequestException(f"알 수 없는 조명: {light_preset}")
    return engine, theme, light_preset


def _spawn_render_job(
    travel_data_path: Path,
    bgm_path: Path | None,
    quick: bool,
    engine: str,
    theme: str,
    light_preset: str,
    intro: bool,
    outro: bool,
    save_as_reels: bool = False,
    user_idx: int | None = None,
) -> dict[str, object]:
    """렌더 서브프로세스를 백그라운드 스레드로 띄우고 job 상태를 반환한다.

    save_as_reels=True 면 렌더 완료 후 결과 영상을 GCS 버킷(reels/)에 올리고
    reels 테이블에 등록한다 (사진만 렌더 자동 릴스화). user_idx 가 있으면 작성자로
    붙여 무작위 추천에서 프로필 사진을 함께 내려줄 수 있다.
    """
    command, marker = _build_command(
        travel_data_path, engine, quick, theme, light_preset, intro, outro
    )
    job = {
        "save_as_reels": save_as_reels,
        "user_idx": user_idx,
        "reels_idx": None,
        "reels_url": None,
        "job_id": uuid.uuid4().hex[:12],
        "status": "running",
        "phase": "렌더 준비 중",
        "percent": 0.0,
        "frame": 0,
        "total_frames": None,
        "started_at": time.time(),
        "elapsed_seconds": 0.0,
        "eta_seconds": None,
        "engine": engine,
        "theme": theme,
        "light_preset": light_preset or None,
        "intro": intro,
        "outro": outro,
        "bgm": bgm_path.name if bgm_path else None,
        "video_url": None,
        "error": None,
        "log_tail": "",
    }
    with _jobs_lock:
        _jobs[job["job_id"]] = job
    threading.Thread(
        target=_run_render_job, args=(job, command, marker), daemon=True
    ).start()
    return _job_snapshot(job)


def start_render(
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
    """입력을 검증하고 렌더 작업을 시작한 뒤 job 상태(dict)를 즉시 반환한다."""
    engine, theme, light_preset = _validate_render_options(engine, theme, light_preset)
    travel_data_path, bgm_path = _build_travel_data(points_json, photo_points_json, photos, bgm)
    return _spawn_render_job(
        travel_data_path, bgm_path, quick, engine, theme, light_preset, intro, outro
    )


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


def start_render_photos_only(
    photos: list[tuple[str, bytes]],
    user_idx: int,
    bgm: str = "",
    quick: bool = False,
    engine: str = "local",
    theme: str = "default",
    light_preset: str = "",
    intro: bool = False,
    outro: bool = False,
    start_name: str = "",
    start_latitude: float | None = None,
    start_longitude: float | None = None,
) -> dict[str, object]:
    """사진들의 EXIF(GPS·촬영시각)만으로 여행 경로 영상 렌더링을 시작한다.

    촬영 시각 순으로 지점을 이동하며 각 지점에서 해당 사진을 보여준다.
    GPS 없는 사진은 제외한다. 지점 기준점(묶음 첫 사진 위치)에서 PHOTO_CLUSTER_KM(1km)
    미만인 연속 사진은 이동 없이 그 지점에 고정해 사진1→사진2→사진3 순으로 보여주고,
    1km 이상 떨어진 사진이 나오면 그 위치로 이동한다.

    start_latitude/longitude 를 주면 그 위치(예: 서울역)를 출발지로 삼아
    첫 사진 지점으로 이동하며 시작한다 (출발지에서는 사진 없이 라벨만 표시).
    """
    engine, theme, light_preset = _validate_render_options(engine, theme, light_preset)

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
    tagged.sort(key=lambda item: (item[2]["taken"] is None, item[2]["taken"] or datetime.min))

    job_dir = UPLOADS_DIR / uuid.uuid4().hex[:12]
    job_dir.mkdir(parents=True, exist_ok=True)

    track_points: list[dict[str, object]] = []
    media_points: list[dict[str, object]] = []
    for order, (filename, content, meta) in enumerate(tagged):
        suffix = Path(filename or "").suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            suffix = ".jpg"
        saved = job_dir / f"photo_{order}{suffix}"
        saved.write_bytes(content)
        rel = saved.relative_to(VIDEO_MAKER_DIR).as_posix()

        if track_points and _haversine_km(
            float(track_points[-1]["latitude"]), float(track_points[-1]["longitude"]),
            float(meta["latitude"]), float(meta["longitude"]),
        ) < PHOTO_CLUSTER_KM:
            # 같은 장소에서 연속 촬영 → 직전 지점의 사진 묶음에 추가.
            media_points[-1]["photos"].append(rel)
            continue

        point: dict[str, object] = {
            "latitude": meta["latitude"],
            "longitude": meta["longitude"],
        }
        if meta["taken"] is not None:
            point["timestamp"] = meta["taken"].isoformat()
        track_points.append(point)
        media_points.append(
            {"trackIndex": len(track_points) - 1, "name": f"지점 {len(track_points)}", "photos": [rel]}
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
    return _spawn_render_job(
        travel_data_path, bgm_path, quick, engine, theme, light_preset, intro, outro,
        save_as_reels=True, user_idx=user_idx,
    )


def get_render_job(job_id: str) -> dict[str, object]:
    """렌더 작업의 현재 상태를 반환한다 (진행률·경과·예상 남은 시간 포함)."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise NotFoundException("렌더 작업을 찾을 수 없습니다.")
    return _job_snapshot(job)


def _job_snapshot(job: dict) -> dict[str, object]:
    with _jobs_lock:
        snapshot = dict(job)
    if snapshot["status"] == "running":
        snapshot["elapsed_seconds"] = round(time.time() - snapshot["started_at"], 1)
        # 초반(5% 미만)은 표본이 적어 ETA 가 크게 튀므로 생략.
        if snapshot["percent"] >= 5:
            remaining = snapshot["elapsed_seconds"] * (100 - snapshot["percent"]) / snapshot["percent"]
            snapshot["eta_seconds"] = round(remaining, 1)
    snapshot.pop("started_at", None)
    snapshot.pop("save_as_reels", None)  # 내부 플래그 — 응답에서 제외
    snapshot.pop("user_idx", None)  # 내부용(릴스 작성자) — 응답에서 제외
    return snapshot


def _run_render_job(job: dict, command: list[str], marker: str) -> None:
    """렌더 서브프로세스를 돌리며 stdout 마커로 job 진행률을 갱신한다 (스레드)."""

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

    output_name = _parse_output_name(stdout, marker)
    if output_name is None:
        update(
            status="failed",
            error="출력 파일 경로를 확인할 수 없습니다.",
            elapsed_seconds=elapsed,
            log_tail=stdout[-2000:],
        )
        return

    log_tail = stdout[-2000:]
    reels_fields: dict[str, object] = {}
    if job.get("save_as_reels"):
        update(percent=99.0, phase="릴스 등록(버킷 업로드)")
        try:
            reels_fields = _register_render_as_reels(
                OUTPUT_DIR / output_name, user_idx=job.get("user_idx")
            )
        except Exception as error:  # 릴스 등록 실패해도 렌더 자체는 성공으로 처리
            logger.exception("렌더 결과 릴스 등록 실패: %s", output_name)
            log_tail += f"\n[warn] 릴스 등록 실패: {error}"

    update(
        status="done",
        phase="완료",
        percent=100.0,
        video_url=f"/api/videos/output/{output_name}",
        elapsed_seconds=round(time.time() - job["started_at"], 1),
        eta_seconds=0.0,
        log_tail=log_tail,
        **reels_fields,
    )


def _register_render_as_reels(
    video_path: Path, user_idx: int | None = None
) -> dict[str, object]:
    """완성 영상을 GCS 버킷(reels/)에 올리고 reels 행을 등록한다.

    user_idx 는 로그인한 작성자(무작위 추천에서 프로필 사진을 붙이는 근거)로 채운다.
    """
    from databases.database import SessionLocal

    url = gcs.upload_bytes(
        f"reels/{uuid.uuid4().hex}.mp4", video_path.read_bytes(), "video/mp4"
    )
    db = SessionLocal()
    try:
        row = reels_dao.create(db, user_idx=user_idx, url=url, title=None)
        db.commit()
        return {"reels_idx": row.reels_idx, "reels_url": url}
    except Exception:
        db.rollback()
        gcs.delete_object(f"reels/{url.rsplit('/', 1)[-1]}")  # 고아 객체 정리
        raise
    finally:
        db.close()
