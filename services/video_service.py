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
import threading
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
    """영상 메타데이터 조회: {duration, has_audio, width, height}."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise ExternalServiceException("ffprobe를 찾을 수 없습니다 (PATH 확인).")
    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if result.returncode != 0:
        raise ExternalServiceException(f"영상 정보를 읽지 못했습니다:\n{(result.stderr or '')[-500:]}")
    info = json.loads(result.stdout or "{}")
    streams = info.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {
        "duration": float(info.get("format", {}).get("duration", 0) or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "width": int(video_stream.get("width", 0) or 0),
        "height": int(video_stream.get("height", 0) or 0),
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

    filters: list[str] = []
    video_labels = audio_labels = ""
    for i, (seg_start, seg_end) in enumerate(keep):
        rng = f"start={seg_start:.3f}" + (f":end={seg_end:.3f}" if seg_end is not None else "")
        filters.append(f"[0:v]trim={rng},setpts=PTS-STARTPTS[v{i}]")
        video_labels += f"[v{i}]"
        if info["has_audio"]:
            filters.append(f"[0:a]atrim={rng},asetpts=PTS-STARTPTS[a{i}]")
            audio_labels += f"[a{i}]"

    maps = ["-map", "[v]"]
    audio_args: list[str] = []
    if info["has_audio"]:
        filters.append(f"{video_labels}{audio_labels}concat=n={len(keep)}:v=1:a=1[v][a]")
        maps += ["-map", "[a]"]
        audio_args = ["-c:a", "aac", "-b:a", "192k"]
    else:
        filters.append(f"{video_labels}concat=n={len(keep)}:v=1:a=0[v]")

    target = _edited_output_path(source, "cut")
    started = time.perf_counter()
    _run_ffmpeg([
        "-i", str(source),
        "-filter_complex", ";".join(filters),
        *maps, *_EDIT_VIDEO_ARGS, *audio_args,
        str(target),
    ])
    return _edit_result(started, target)


def overlay_image(
    name: str,
    start_seconds: float,
    end_seconds: float,
    image_filename: str,
    image_bytes: bytes,
) -> dict[str, object]:
    """완성 영상의 [start, end) 구간에 이미지를 중앙 오버레이한 새 영상을 만든다."""
    source = get_output_path(name)
    info = _ffprobe_video(source)
    duration = float(info["duration"])
    start, end = float(start_seconds), float(end_seconds)
    if start < 0 or end <= start:
        raise BadRequestException("삽입 구간이 올바르지 않습니다 (0 ≤ 시작 < 끝).")
    if start >= duration:
        raise BadRequestException(f"시작 시각이 영상 길이({duration:.1f}초)를 넘습니다.")
    end = min(end, duration)

    suffix = Path(image_filename or "").suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        raise BadRequestException("이미지 파일이 아닙니다 (jpg/png/webp 등).")
    if not image_bytes:
        raise BadRequestException("이미지 파일이 비어 있습니다.")

    # 영상 프레임의 86% 박스 안에 비율 유지로 맞춰 중앙에 얹는다.
    box_w = max(2, int(int(info["width"]) * 0.86)) if info["width"] else -1
    box_h = max(2, int(int(info["height"]) * 0.86)) if info["height"] else -1
    temp_dir = VIDEO_MAKER_DIR / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    image_path = temp_dir / f"overlay_{uuid.uuid4().hex[:8]}{suffix}"
    image_path.write_bytes(image_bytes)

    target = _edited_output_path(source, "img")
    started = time.perf_counter()
    filter_complex = (
        f"[1:v]scale={box_w}:{box_h}:force_original_aspect_ratio=decrease[img];"
        f"[0:v][img]overlay=(W-w)/2:(H-h)/2:enable='between(t,{start:.3f},{end:.3f})'[v]"
    )
    audio_args = ["-map", "0:a", "-c:a", "copy"] if info["has_audio"] else []
    try:
        _run_ffmpeg([
            "-i", str(source), "-i", str(image_path),
            "-filter_complex", filter_complex,
            "-map", "[v]", *audio_args, *_EDIT_VIDEO_ARGS,
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
    engine = (engine or "local").lower().strip()
    if engine not in {"local", "modal"}:
        raise BadRequestException(f"알 수 없는 엔진: {engine}")
    theme = (theme or "default").lower().strip()
    if theme not in ALLOWED_THEMES:
        raise BadRequestException(f"알 수 없는 테마: {theme}")
    light_preset = (light_preset or "").lower().strip()
    if light_preset not in ALLOWED_LIGHT_PRESETS:
        raise BadRequestException(f"알 수 없는 조명: {light_preset}")

    # 검증 오류(400)는 여기서 동기적으로 발생시키고, 스레드는 그 뒤에 띄운다.
    travel_data_path, bgm_path = _build_travel_data(points_json, photo_points_json, photos, bgm)
    command, marker = _build_command(
        travel_data_path, engine, quick, theme, light_preset, intro, outro
    )

    job = {
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

    update(
        status="done",
        phase="완료",
        percent=100.0,
        video_url=f"/api/videos/output/{output_name}",
        elapsed_seconds=elapsed,
        eta_seconds=0.0,
        log_tail=stdout[-2000:],
    )
