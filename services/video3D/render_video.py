from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageOps
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
ASSETS_DIR = ROOT / "assets"
FRAMES_DIR = ROOT / "frames"
OUTPUT_DIR = ROOT / "output"
TEMP_DIR = ROOT / "temp"
DEBUG_DIR = ROOT / "debug"
CHROMIUM_PROFILE_DIR = TEMP_DIR / "chromium_profile"
PHOTO_PATH = ASSETS_DIR / "destination_photo.jpg"
HTML_PATH = ROOT / "map.html"
CHROMIUM_BASE_ARGS = [
    "--enable-webgl",
    "--disable-extensions",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--no-first-run",
    "--no-default-browser-check",
]

SOFTWARE_RENDERER_TERMS = ("swiftshader", "llvmpipe", "software", "basic render")


def build_chromium_args(gpu_mode: str, disable_software_rasterizer: bool = False) -> list[str]:
    """Assemble Chromium launch args for the requested GPU backend.

    On Windows the single decisive flag is ``--use-angle=d3d11``: without it,
    headless Chromium silently falls back to the SwiftShader software renderer
    even when a real GPU and ``--ignore-gpu-blocklist`` are present.
    """
    args = list(CHROMIUM_BASE_ARGS)
    if not sys.platform.startswith("win"):
        # 리눅스 컨테이너(예: Modal)에서 root 로 실행 시 필요. /dev/shm 이 작아도 안전.
        args += ["--no-sandbox", "--disable-dev-shm-usage"]
    if gpu_mode == "swiftshader":
        # Force the software path (useful as an explicit baseline / fallback).
        args += ["--use-angle=swiftshader"]
        return args

    # GPU-accelerated modes (auto / d3d11 / default).
    args += [
        "--ignore-gpu-blocklist",
        "--enable-gpu-rasterization",
        "--enable-zero-copy",
    ]
    if gpu_mode in ("auto", "d3d11"):
        if sys.platform.startswith("win"):
            args += ["--use-angle=d3d11"]
        else:
            # 리눅스/클라우드 GPU(NVIDIA T4 검증 완료): d3d11 은 존재하지 않아
            # SwiftShader 로 폴백한다. vulkan 백엔드가 실제 GPU 를 잡는다.
            args += ["--use-angle=vulkan", "--enable-features=Vulkan"]
    # gpu_mode == "default": let Chromium pick the ANGLE backend itself.
    if disable_software_rasterizer:
        args += ["--disable-software-rasterizer"]
    return args


def is_software_renderer(renderer: str) -> bool:
    lowered = renderer.lower()
    return any(term in lowered for term in SOFTWARE_RENDERER_TERMS)

TOKEN_MISSING_MESSAGE = (
    "MAPBOX_ACCESS_TOKEN이 없습니다.\n"
    ".env.example을 복사해 .env를 만들고 Mapbox public access token을 설정하세요."
)


@dataclass(frozen=True)
class RenderConfig:
    width: int
    height: int
    fps: int
    map_seconds: float
    move_seconds_per_km: float
    max_video_seconds: float
    photo_seconds: float
    fade_seconds: float
    black_fade_seconds: float
    stop_seconds: float
    arrival_hold_seconds: float
    photo_fade_in_seconds: float
    photo_hold_seconds: float
    photo_fade_out_seconds: float
    stabilize_ms: int
    mode_name: str


DEFAULT_CONFIG = RenderConfig(
    width=1080,
    height=1920,
    fps=30,
    map_seconds=18.0,
    move_seconds_per_km=0.05,
    max_video_seconds=60.0,
    photo_seconds=1.8,
    fade_seconds=0.6,
    black_fade_seconds=0.25,
    stop_seconds=0.8,
    arrival_hold_seconds=1.5,
    photo_fade_in_seconds=0.4,
    photo_hold_seconds=1.6,
    photo_fade_out_seconds=0.4,
    stabilize_ms=80,
    mode_name="default",
)

QUALITY_FAST_CONFIG = RenderConfig(
    width=1080,
    height=1920,
    fps=30,
    map_seconds=18.0,
    move_seconds_per_km=0.05,
    max_video_seconds=60.0,
    photo_seconds=1.8,
    fade_seconds=0.6,
    black_fade_seconds=0.25,
    stop_seconds=0.8,
    arrival_hold_seconds=1.5,
    photo_fade_in_seconds=0.4,
    photo_hold_seconds=1.6,
    photo_fade_out_seconds=0.4,
    stabilize_ms=80,
    mode_name="quality-fast",
)

QUICK_CONFIG = RenderConfig(
    width=540,
    height=960,
    fps=15,
    map_seconds=9.0,
    move_seconds_per_km=0.05,
    max_video_seconds=60.0,
    photo_seconds=1.0,
    fade_seconds=0.4,
    black_fade_seconds=0.2,
    stop_seconds=0.5,
    arrival_hold_seconds=1.5,
    photo_fade_in_seconds=0.25,
    photo_hold_seconds=0.8,
    photo_fade_out_seconds=0.25,
    stabilize_ms=50,
    mode_name="quick",
)


@dataclass(frozen=True)
class TrackPoint:
    latitude: float
    longitude: float
    timestamp: str | None = None


@dataclass(frozen=True)
class MediaPoint:
    track_index: int
    name: str
    photos: tuple[Path, ...]


@dataclass(frozen=True)
class TimelineSegment:
    type: str
    duration: float
    start_track_index: int | None = None
    end_track_index: int | None = None
    track_index: int | None = None
    name: str | None = None
    photo_path: Path | None = None
    fade_in_seconds: float = 0.0
    hold_seconds: float = 0.0
    fade_out_seconds: float = 0.0


DEFAULT_TRAVEL_DATA = {
    "trackPoints": [
        {"name": "서울역", "longitude": 126.9706, "latitude": 37.5547},
        {"name": "대전", "longitude": 127.3845, "latitude": 36.3504},
        {"name": "대구", "longitude": 128.6014, "latitude": 35.8714},
        {"name": "부산역", "longitude": 129.0403, "latitude": 35.1151},
    ],
    "mediaPoints": [
        {"trackIndex": 0, "name": "서울역", "photos": []},
        {"trackIndex": 3, "name": "부산역", "photos": ["assets/destination_photo.jpg"]},
    ],
}


BEARING_TEST_TRAVEL_DATA = {
    "trackPoints": [
        {"longitude": 127.0100, "latitude": 37.5400},
        {"longitude": 127.0750, "latitude": 37.5400},
        {"longitude": 127.0750, "latitude": 37.4850},
        {"longitude": 127.0050, "latitude": 37.4850},
        {"longitude": 127.0550, "latitude": 37.4450},
    ],
    "mediaPoints": [
        {"trackIndex": 0, "name": "Bearing test start", "photos": []},
        {"trackIndex": 4, "name": "Southeast finish", "photos": []},
    ],
}


def parse_bool(value: str) -> bool:
    lowered = value.lower()
    if lowered in {"1", "true", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError("Expected true or false")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a deterministic Mapbox GL JS 3D travel route video."
    )
    parser.add_argument("--quick", action="store_true", help="빠른 테스트 모드로 렌더링")
    parser.add_argument(
        "--quality-fast",
        action="store_true",
        help="품질 유지 + 5분 이내 목표 프리셋 (1080x1920, cdp-jpeg, GPU)",
    )
    parser.add_argument("--width", type=int, help="출력 너비")
    parser.add_argument("--height", type=int, help="출력 높이")
    parser.add_argument("--fps", type=int, help="출력 FPS")
    parser.add_argument("--map-seconds", type=float, help="3D 지도 구간 길이(폴백/레거시 경로용)")
    parser.add_argument(
        "--move-seconds-per-km",
        type=float,
        help="이동 속도(거리 비례). km당 초. 기본 0.05 (작을수록 빠름)",
    )
    parser.add_argument(
        "--max-video-seconds",
        type=float,
        help="전체 영상 최대 길이(초). 초과하면 이동 구간을 압축해 맞춤. 기본 60",
    )
    parser.add_argument("--photo-seconds", type=float, help="목적지 사진 유지 시간")
    parser.add_argument("--travel-data", type=Path, help="trackPoints/mediaPoints JSON 파일")
    parser.add_argument(
        "--bgm",
        type=Path,
        default=None,
        help="배경음악(mp3 등) 파일 경로. travel data의 bgm 필드보다 우선합니다.",
    )
    parser.add_argument("--stop-seconds", type=float, help="사진 지점 지도 정지 시간")
    parser.add_argument(
        "--arrival-hold-seconds",
        type=float,
        help="사진이 없는 지점에 도착했을 때 머무는 최소 시간 (기본 1.5초)",
    )
    parser.add_argument("--photo-fade-in-seconds", type=float, help="사진 fade in 시간")
    parser.add_argument("--photo-hold-seconds", type=float, help="사진 hold 시간")
    parser.add_argument("--photo-fade-out-seconds", type=float, help="사진 fade out 시간")
    parser.add_argument("--save-frames", action="store_true", help="PNG 프레임 시퀀스를 frames 폴더에 저장")
    parser.add_argument("--x264-preset", default="veryfast", help="FFmpeg libx264 preset")
    parser.add_argument("--crf", type=int, default=18, help="FFmpeg libx264 CRF")
    parser.add_argument("--benchmark-frames", type=int, help="첫 N개 지도 프레임만 렌더링해 성능 측정")
    parser.add_argument(
        "--capture-mode",
        choices=["playwright-png", "cdp-png", "cdp-jpeg"],
        default=None,
        help="지도 프레임 캡처 방식 (미지정 시 preset 기본값)",
    )
    parser.add_argument(
        "--render-wait-mode",
        choices=["map-render", "raf", "none"],
        default=None,
        help="renderFrame 이후 대기 방식 (미지정 시 preset 기본값)",
    )
    parser.add_argument("--jpeg-quality", type=int, default=None, help="cdp-jpeg 캡처 품질")
    parser.add_argument(
        "--capture-from-surface",
        type=parse_bool,
        default=True,
        help="CDP Page.captureScreenshot fromSurface 값",
    )
    parser.add_argument("--warmup", action="store_true", help="렌더 전 타일 워밍업 실행")
    parser.add_argument("--warmup-samples", type=int, default=5, help="워밍업 샘플 수")
    parser.add_argument("--warmup-timeout-ms", type=int, default=500, help="워밍업 지점별 idle timeout")
    parser.add_argument("--warmup-total-timeout-ms", type=int, default=3000, help="워밍업 전체 timeout")
    parser.add_argument("--queue-size", type=int, default=None, help="FFmpeg writer queue 크기")
    parser.add_argument("--fast", action="store_true", help="AWS 저부하 고속 모드")
    parser.add_argument(
        "--gpu-mode",
        choices=["auto", "d3d11", "default", "swiftshader"],
        default="auto",
        help="WebGL 렌더 백엔드 선택 (auto=d3d11 GPU 가속, swiftshader=소프트웨어)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="headless 대신 headed 모드로 Chromium 실행 (GPU 비교용)",
    )
    parser.add_argument(
        "--disable-software-rasterizer",
        action="store_true",
        help="--disable-software-rasterizer 추가 (GPU 강제, 실패 시 렌더 오류)",
    )
    parser.add_argument(
        "--use-persistent-context",
        action="store_true",
        help="temp/chromium_profile persistent context 사용",
    )
    parser.add_argument(
        "--bearing-test-route",
        action="store_true",
        help="bearing smoothing 확인용 짧은 급회전 경로 사용",
    )
    args = parser.parse_args()

    # Preset defaults fill ONLY options the user did not pass explicitly, so an
    # explicit CLI value always wins over --fast / --quality-fast defaults.
    if args.capture_mode is None:
        args.capture_mode = "cdp-jpeg" if (args.fast or args.quality_fast) else "cdp-png"
    if args.render_wait_mode is None:
        # quality-fast/fast favor speed but stay safe with "raf"; classic default
        # keeps the strict "map-render" wait.
        args.render_wait_mode = "raf" if (args.fast or args.quality_fast) else "map-render"
    if args.jpeg_quality is None:
        args.jpeg_quality = 95
    if args.queue_size is None:
        args.queue_size = 2 if args.fast else 3
    if args.fast and not args.warmup:
        args.warmup = False
    return args


def build_config(args: argparse.Namespace) -> RenderConfig:
    if args.quick:
        base = QUICK_CONFIG
    elif args.quality_fast:
        base = QUALITY_FAST_CONFIG
    else:
        base = DEFAULT_CONFIG
    return RenderConfig(
        width=args.width or base.width,
        height=args.height or base.height,
        fps=args.fps or base.fps,
        map_seconds=args.map_seconds or base.map_seconds,
        move_seconds_per_km=(
            args.move_seconds_per_km
            if args.move_seconds_per_km is not None
            else base.move_seconds_per_km
        ),
        max_video_seconds=(
            args.max_video_seconds
            if args.max_video_seconds is not None
            else base.max_video_seconds
        ),
        photo_seconds=args.photo_seconds or base.photo_seconds,
        fade_seconds=base.fade_seconds,
        black_fade_seconds=base.black_fade_seconds,
        stop_seconds=args.stop_seconds if args.stop_seconds is not None else base.stop_seconds,
        arrival_hold_seconds=(
            args.arrival_hold_seconds
            if args.arrival_hold_seconds is not None
            else base.arrival_hold_seconds
        ),
        photo_fade_in_seconds=(
            args.photo_fade_in_seconds
            if args.photo_fade_in_seconds is not None
            else base.photo_fade_in_seconds
        ),
        photo_hold_seconds=(
            args.photo_hold_seconds
            if args.photo_hold_seconds is not None
            else (args.photo_seconds if args.photo_seconds is not None else base.photo_hold_seconds)
        ),
        photo_fade_out_seconds=(
            args.photo_fade_out_seconds
            if args.photo_fade_out_seconds is not None
            else base.photo_fade_out_seconds
        ),
        stabilize_ms=base.stabilize_ms,
        mode_name=base.mode_name,
    )


def ensure_directories() -> None:
    for directory in (ASSETS_DIR, FRAMES_DIR, OUTPUT_DIR, TEMP_DIR, DEBUG_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def load_token() -> str | None:
    load_dotenv(ROOT / ".env")
    token = os.getenv("MAPBOX_ACCESS_TOKEN")
    if not token or token.strip() == "":
        return None
    return token.strip()


def find_font() -> Path | None:
    candidates = [
        # Windows (로컬 개발)
        Path("C:/Windows/Fonts/malgun.ttf"),
        Path("C:/Windows/Fonts/malgunbd.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/segoeui.ttf"),
        # Linux 컨테이너(예: Modal) — 한글 라벨용 CJK 폰트. 위 Windows 경로가
        # 없으면 여기로 폴백한다. 이미지에 fonts-noto-cjk / fonts-nanum 설치 필요.
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_font(size: int, font_path: Path | None) -> ImageFont.ImageFont:
    if font_path:
        try:
            return ImageFont.truetype(str(font_path), size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def draw_centered_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    draw.text((xy[0] - width / 2, xy[1] - height / 2), text, font=font, fill=fill)


def ensure_destination_photo(path: Path = PHOTO_PATH) -> None:
    if path.exists():
        return

    width, height = 1080, 1920
    image = Image.new("RGB", (width, height), "#0f172a")
    draw = ImageDraw.Draw(image)

    top = (30, 64, 175)
    mid = (15, 118, 110)
    bottom = (249, 115, 22)
    for y in range(height):
        ratio = y / (height - 1)
        if ratio < 0.58:
            local = ratio / 0.58
            color = tuple(int(top[i] + (mid[i] - top[i]) * local) for i in range(3))
        else:
            local = (ratio - 0.58) / 0.42
            color = tuple(int(mid[i] + (bottom[i] - mid[i]) * local) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color)

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    odraw.ellipse((-180, 150, 470, 800), fill=(255, 255, 255, 34))
    odraw.ellipse((650, 240, 1280, 880), fill=(20, 184, 166, 48))
    odraw.ellipse((120, 1230, 960, 2060), fill=(255, 255, 255, 26))
    odraw.rounded_rectangle((96, 1010, 984, 1670), radius=42, fill=(255, 255, 255, 222))
    odraw.rounded_rectangle((144, 1090, 936, 1370), radius=36, fill=(15, 23, 42, 235))
    odraw.rectangle((0, 1510, width, height), fill=(2, 6, 23, 62))
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(image)

    font_path = find_font()
    use_korean = font_path is not None and font_path.name.lower().startswith("malgun")
    title = "부산역" if use_korean else "BUSAN STATION"
    subtitle = "Seoul to Busan" if use_korean else "SEOUL TO BUSAN"
    date_text = date.today().strftime("%Y.%m.%d")

    title_font = load_font(132, font_path)
    sub_font = load_font(42, font_path)
    small_font = load_font(34, font_path)
    label_font = load_font(58, font_path)

    draw_centered_text(draw, (width // 2, 580), "BUSAN", title_font, (255, 255, 255))
    draw_centered_text(draw, (width // 2, 705), "TRAVEL ROUTE", sub_font, (226, 232, 240))
    draw_centered_text(draw, (width // 2, 1165), title, label_font, (248, 250, 252))
    draw_centered_text(draw, (width // 2, 1240), subtitle, small_font, (125, 211, 252))

    draw.line((210, 1445, 870, 1445), fill=(15, 23, 42), width=3)
    draw_centered_text(draw, (width // 2, 1515), date_text, small_font, (15, 23, 42))

    for x, y, radius, color in [
        (255, 1550, 18, (14, 165, 233)),
        (395, 1490, 12, (34, 197, 94)),
        (720, 1545, 16, (239, 68, 68)),
        (825, 1480, 10, (250, 204, 21)),
    ]:
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, quality=92)


def resolve_project_path(path: Path, source_path: Path | None = None) -> Path:
    if path.is_absolute():
        return path

    root_candidate = (ROOT / path).resolve()
    if root_candidate.exists() or str(path).replace("\\", "/").startswith("assets/"):
        return root_candidate

    if source_path is not None:
        source_candidate = (source_path.parent / path).resolve()
        if source_candidate.exists():
            return source_candidate

    return root_candidate


def load_raw_travel_data(
    travel_data_path: Path | None,
    use_bearing_test_route: bool,
) -> tuple[dict[str, object], Path | None, str]:
    if use_bearing_test_route:
        return BEARING_TEST_TRAVEL_DATA, None, "bearing-test"

    if travel_data_path is None:
        return DEFAULT_TRAVEL_DATA, None, "fallback"

    resolved_path = resolve_project_path(travel_data_path)
    if not resolved_path.exists():
        raise RuntimeError(f"Travel data file not found: {resolved_path}")

    try:
        with resolved_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid travel data JSON: {resolved_path}: {error}") from error

    if not isinstance(data, dict):
        raise RuntimeError("Travel data root must be a JSON object.")

    return data, resolved_path, str(travel_data_path)


def parse_track_point(raw_point: object, index: int) -> TrackPoint:
    if not isinstance(raw_point, dict):
        raise RuntimeError(f"trackPoints[{index}] must be an object.")

    try:
        latitude = float(raw_point["latitude"])
        longitude = float(raw_point["longitude"])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(
            f"trackPoints[{index}] must include numeric latitude and longitude."
        ) from error

    if not -90 <= latitude <= 90:
        raise RuntimeError(f"trackPoints[{index}].latitude out of range: {latitude}")
    if not -180 <= longitude <= 180:
        raise RuntimeError(f"trackPoints[{index}].longitude out of range: {longitude}")

    timestamp = raw_point.get("timestamp")
    return TrackPoint(
        latitude=latitude,
        longitude=longitude,
        timestamp=str(timestamp) if timestamp is not None else None,
    )


def validate_travel_data(
    raw_data: dict[str, object],
    source_path: Path | None,
) -> tuple[list[TrackPoint], list[MediaPoint]]:
    raw_track_points = raw_data.get("trackPoints")
    if not isinstance(raw_track_points, list):
        raise RuntimeError("travel data must include trackPoints as an array.")
    if len(raw_track_points) < 2:
        raise RuntimeError("trackPoints must include at least 2 points.")

    track_points = [
        parse_track_point(raw_point, index)
        for index, raw_point in enumerate(raw_track_points)
    ]

    raw_media_points = raw_data.get("mediaPoints", [])
    if raw_media_points is None:
        raw_media_points = []
    if not isinstance(raw_media_points, list):
        raise RuntimeError("mediaPoints must be an array when provided.")

    media_by_index: dict[int, MediaPoint] = {}
    for media_index, raw_media_point in enumerate(raw_media_points):
        if not isinstance(raw_media_point, dict):
            print(f"[warn] mediaPoints[{media_index}] is not an object; skipped")
            continue

        try:
            track_index = int(raw_media_point["trackIndex"])
        except (KeyError, TypeError, ValueError):
            print(f"[warn] mediaPoints[{media_index}] has invalid trackIndex; skipped")
            continue

        if not 0 <= track_index < len(track_points):
            print(
                "[warn] "
                f"mediaPoints[{media_index}].trackIndex out of range: {track_index}; skipped"
            )
            continue

        raw_name = raw_media_point.get("name")
        name = str(raw_name).strip() if raw_name is not None else f"Track {track_index}"
        if not name:
            name = f"Track {track_index}"

        raw_photos = raw_media_point.get("photos", [])
        if raw_photos is None:
            raw_photos = []
        if not isinstance(raw_photos, list):
            print(
                "[warn] "
                f"mediaPoints[{media_index}].photos must be an array; photos skipped"
            )
            raw_photos = []

        valid_photos: list[Path] = []
        for photo_index, raw_photo in enumerate(raw_photos):
            if not isinstance(raw_photo, str) or not raw_photo.strip():
                print(
                    "[warn] "
                    f"mediaPoints[{media_index}].photos[{photo_index}] is invalid; skipped"
                )
                continue

            photo_path = resolve_project_path(Path(raw_photo), source_path)
            if not photo_path.exists():
                print(f"[warn] photo file not found, skipped: {raw_photo}")
                continue

            valid_photos.append(photo_path)

        existing = media_by_index.get(track_index)
        if existing is not None:
            if existing.name != name:
                print(
                    "[warn] "
                    f"duplicate mediaPoint trackIndex={track_index}; keeping name '{existing.name}'"
                )
            media_by_index[track_index] = MediaPoint(
                track_index=track_index,
                name=existing.name,
                photos=existing.photos + tuple(valid_photos),
            )
            continue

        media_by_index[track_index] = MediaPoint(
            track_index=track_index,
            name=name,
            photos=tuple(valid_photos),
        )

    return track_points, sorted(media_by_index.values(), key=lambda point: point.track_index)


def path_for_json(path: Path) -> str:
    try:
        display_path = path.resolve().relative_to(ROOT.resolve())
    except ValueError:
        display_path = path
    return str(display_path).replace("\\", "/")


def travel_data_for_browser(
    track_points: list[TrackPoint],
    media_points: list[MediaPoint],
) -> dict[str, object]:
    return {
        "trackPoints": [
            {
                "latitude": point.latitude,
                "longitude": point.longitude,
                "timestamp": point.timestamp,
            }
            for point in track_points
        ],
        "mediaPoints": [
            {
                "trackIndex": point.track_index,
                "name": point.name,
                "photos": [path_for_json(photo) for photo in point.photos],
            }
            for point in media_points
        ],
    }


def haversine_km_points(a: TrackPoint, b: TrackPoint) -> float:
    radius_km = 6371.0088
    lat1 = math.radians(a.latitude)
    lat2 = math.radians(b.latitude)
    d_lat = math.radians(b.latitude - a.latitude)
    d_lon = math.radians(b.longitude - a.longitude)
    sin_lat = math.sin(d_lat / 2)
    sin_lon = math.sin(d_lon / 2)
    value = sin_lat * sin_lat + math.cos(lat1) * math.cos(lat2) * sin_lon * sin_lon
    value = min(1.0, max(0.0, value))
    return radius_km * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def path_distance_km(
    track_points: list[TrackPoint],
    start_track_index: int,
    end_track_index: int,
) -> float:
    start = max(0, min(start_track_index, len(track_points) - 1))
    end = max(0, min(end_track_index, len(track_points) - 1))
    if end <= start:
        return 0.0
    return sum(
        haversine_km_points(track_points[index], track_points[index + 1])
        for index in range(start, end)
    )


def allocate_move_durations(distances_km: list[float], total_seconds: float) -> list[float]:
    if not distances_km:
        return []

    segment_count = len(distances_km)
    total_seconds = max(0.1 * segment_count, total_seconds)
    min_seconds = min(0.45, total_seconds / segment_count)
    floor_total = min_seconds * segment_count
    variable_seconds = max(0.0, total_seconds - floor_total)
    total_distance = sum(max(0.0, distance) for distance in distances_km)

    if total_distance <= 0:
        return [total_seconds / segment_count for _ in distances_km]

    return [
        min_seconds + variable_seconds * (max(0.0, distance) / total_distance)
        for distance in distances_km
    ]


def stops_and_photos_seconds(
    stop_points: list[MediaPoint],
    config: RenderConfig,
) -> float:
    """Total non-move time: per-stop holds + per-photo fade/hold sequences.

    Must mirror exactly what build_timeline_segments emits for holds and photos.
    """
    photo_seconds = (
        config.photo_fade_in_seconds
        + config.photo_hold_seconds
        + config.photo_fade_out_seconds
    )
    total = 0.0
    for point in stop_points:
        total += config.stop_seconds if point.photos else config.arrival_hold_seconds
        total += photo_seconds * len(point.photos)
    return total


def build_timeline_segments(
    track_points: list[TrackPoint],
    media_points: list[MediaPoint],
    config: RenderConfig,
) -> list[TimelineSegment]:
    # Stop at every named place the user added, not just the ones with photos,
    # so arrivals get a brief hold instead of being flown straight through.
    stop_points = list(media_points)
    move_specs: list[tuple[int, int, float]] = []
    previous_index = 0

    for media_point in stop_points:
        if media_point.track_index > previous_index:
            move_specs.append(
                (
                    previous_index,
                    media_point.track_index,
                    path_distance_km(track_points, previous_index, media_point.track_index),
                )
            )
        previous_index = media_point.track_index

    last_track_index = len(track_points) - 1
    if previous_index < last_track_index:
        move_specs.append(
            (
                previous_index,
                last_track_index,
                path_distance_km(track_points, previous_index, last_track_index),
            )
        )

    distances = [distance for _, _, distance in move_specs]

    # Distance-based pacing: each move takes `move_seconds_per_km` per km, so the
    # camera keeps a consistent speed no matter how long the route is.
    desired_move_total = sum(distances) * config.move_seconds_per_km

    # Holds + photos are fixed by the points/photos the user added.
    non_move_seconds = stops_and_photos_seconds(stop_points, config)

    # Cap the whole video to max_video_seconds by compressing ONLY the moves
    # (speeding them up like before), leaving holds/photos intact.
    move_total = desired_move_total
    if config.max_video_seconds > 0:
        if desired_move_total + non_move_seconds > config.max_video_seconds:
            move_total = max(0.0, config.max_video_seconds - non_move_seconds)
            print(
                f"[info] 전체 {desired_move_total + non_move_seconds:.1f}s > "
                f"{config.max_video_seconds:.0f}s 상한 → 이동 구간을 "
                f"{desired_move_total:.1f}s에서 {move_total:.1f}s로 압축"
            )

    move_durations = allocate_move_durations(distances, move_total)
    move_duration_iter = iter(move_durations)
    segments: list[TimelineSegment] = []
    previous_index = 0

    for media_point in stop_points:
        if media_point.track_index > previous_index:
            segments.append(
                TimelineSegment(
                    type="map_move",
                    start_track_index=previous_index,
                    end_track_index=media_point.track_index,
                    duration=next(move_duration_iter),
                )
            )

        # Points with photos: short hold (stop_seconds) before the photo sequence,
        # which itself provides dwell time. Points without photos: hold at least
        # arrival_hold_seconds so the place is actually visible before moving on.
        hold_duration = (
            config.stop_seconds if media_point.photos else config.arrival_hold_seconds
        )
        segments.append(
            TimelineSegment(
                type="map_hold",
                track_index=media_point.track_index,
                name=media_point.name,
                duration=hold_duration,
            )
        )

        for photo_path in media_point.photos:
            segments.append(
                TimelineSegment(
                    type="photo",
                    track_index=media_point.track_index,
                    name=media_point.name,
                    photo_path=photo_path,
                    duration=(
                        config.photo_fade_in_seconds
                        + config.photo_hold_seconds
                        + config.photo_fade_out_seconds
                    ),
                    fade_in_seconds=config.photo_fade_in_seconds,
                    hold_seconds=config.photo_hold_seconds,
                    fade_out_seconds=config.photo_fade_out_seconds,
                )
            )

        previous_index = media_point.track_index

    last_track_index = len(track_points) - 1
    if previous_index < last_track_index:
        segments.append(
            TimelineSegment(
                type="map_move",
                start_track_index=previous_index,
                end_track_index=last_track_index,
                duration=next(move_duration_iter),
            )
        )

    if not segments:
        segments.append(
            TimelineSegment(
                type="map_move",
                start_track_index=0,
                end_track_index=last_track_index,
                duration=config.map_seconds,
            )
        )

    return segments


def frame_count_for_seconds(seconds: float, fps: int) -> int:
    if seconds <= 0:
        return 0
    return max(1, round(seconds * fps))


def timeline_total_frames(segments: list[TimelineSegment], config: RenderConfig) -> int:
    return sum(frame_count_for_seconds(segment.duration, config.fps) for segment in segments)


def print_timeline_summary(segments: list[TimelineSegment], config: RenderConfig) -> None:
    map_seconds = sum(segment.duration for segment in segments if segment.type != "photo")
    photo_seconds = sum(segment.duration for segment in segments if segment.type == "photo")
    print(
        "[timeline] "
        f"segments={len(segments)} map_seconds={map_seconds:.2f} "
        f"photo_seconds={photo_seconds:.2f} total_frames={timeline_total_frames(segments, config)}"
    )


def next_output_path(output_dir: Path = OUTPUT_DIR) -> tuple[Path, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(r"^travel_3d_(\d+)\.mp4$")
    max_id = 0
    for path in output_dir.iterdir():
        match = pattern.match(path.name)
        if match:
            max_id = max(max_id, int(match.group(1)))
    next_id = max_id + 1
    return output_dir / f"travel_3d_{next_id}.mp4", next_id


def clear_previous_frames() -> None:
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    frames_dir = FRAMES_DIR.resolve()
    for frame in list(FRAMES_DIR.glob("frame_*.png")) + list(FRAMES_DIR.glob("frame_*.jpg")):
        if frame.resolve().parent != frames_dir:
            raise RuntimeError(f"Unexpected frame path: {frame}")
        frame.unlink()


def clear_debug_images() -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    debug_dir = DEBUG_DIR.resolve()
    for image in list(DEBUG_DIR.glob("bearing_*.png")) + list(DEBUG_DIR.glob("bearing_*.jpg")):
        if image.resolve().parent != debug_dir:
            raise RuntimeError(f"Unexpected debug image path: {image}")
        image.unlink()


class PerfStats:
    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.stages: dict[str, float] = {}
        self.frame_total_ms: list[float] = []
        self.frame_render_call_ms: list[float] = []
        self.frame_render_wait_ms: list[float] = []
        self.frame_screenshot_ms: list[float] = []
        self.frame_output_ms: list[float] = []

    def add_stage(self, name: str, seconds: float) -> None:
        self.stages[name] = self.stages.get(name, 0.0) + seconds

    def add_frame(
        self,
        total_ms: float,
        render_call_ms: float,
        render_wait_ms: float,
        screenshot_ms: float,
        output_ms: float,
    ) -> None:
        self.frame_total_ms.append(total_ms)
        self.frame_render_call_ms.append(render_call_ms)
        self.frame_render_wait_ms.append(render_wait_ms)
        self.frame_screenshot_ms.append(screenshot_ms)
        self.frame_output_ms.append(output_ms)

    @staticmethod
    def _summary(values: list[float]) -> str:
        if not values:
            return "avg=0.0ms min=0.0ms max=0.0ms"
        return (
            f"avg={sum(values) / len(values):.1f}ms "
            f"min={min(values):.1f}ms max={max(values):.1f}ms"
        )

    def print_report(self) -> None:
        total = time.perf_counter() - self.started
        print("[perf] summary")
        print(f"[perf] total={total:.2f}s")
        for name, seconds in self.stages.items():
            print(f"[perf] {name}={seconds:.2f}s")
        print(f"[perf] map_frame_total {self._summary(self.frame_total_ms)}")
        print(f"[perf] renderFrame_call {self._summary(self.frame_render_call_ms)}")
        print(f"[perf] render_wait_js {self._summary(self.frame_render_wait_ms)}")
        print(f"[perf] screenshot {self._summary(self.frame_screenshot_ms)}")
        print(f"[perf] frame_output {self._summary(self.frame_output_ms)}")


class FfmpegPipeWriter:
    def __init__(
        self,
        ffmpeg: str,
        config: RenderConfig,
        output_path: Path,
        preset: str,
        crf: int,
        input_codec: str,
        queue_size: int = 6,
    ) -> None:
        input_vcodec = "mjpeg" if input_codec == "jpeg" else "png"
        self.command = [
            ffmpeg,
            "-y",
            "-f",
            "image2pipe",
            "-vcodec",
            input_vcodec,
            "-framerate",
            str(config.fps),
            "-i",
            "pipe:0",
            "-vf",
            "scale=in_range=pc:out_range=tv,format=yuv420p"
            if input_codec == "jpeg"
            else "format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(config.fps),
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        self.queue: queue.Queue[bytes | None] = queue.Queue(maxsize=queue_size)
        self.process: subprocess.Popen[bytes] | None = None
        self.writer_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None
        self.stderr_lines: list[str] = []
        self.write_error: BaseException | None = None
        self.closed = False
        self.close_seconds = 0.0

    def start(self) -> None:
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self.stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self.writer_thread.start()
        self.stderr_thread.start()

    def _stderr_loop(self) -> None:
        if not self.process or not self.process.stderr:
            return
        for raw_line in iter(self.process.stderr.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line:
                self.stderr_lines.append(line)
                if len(self.stderr_lines) > 80:
                    self.stderr_lines = self.stderr_lines[-80:]

    def _writer_loop(self) -> None:
        assert self.process is not None
        assert self.process.stdin is not None
        try:
            while True:
                item = self.queue.get()
                if item is None:
                    break
                self.process.stdin.write(item)
        except BaseException as error:
            self.write_error = error
        finally:
            try:
                self.process.stdin.close()
            except OSError:
                pass

    def write_frame(self, png_bytes: bytes, timeout: float = 60.0) -> None:
        if self.write_error is not None:
            raise RuntimeError(f"FFmpeg writer failed: {self.write_error}")
        self.queue.put(png_bytes, timeout=timeout)

    def close(self, timeout: float = 180.0) -> float:
        if self.closed:
            return self.close_seconds

        started = time.perf_counter()
        self.closed = True
        self.queue.put(None, timeout=timeout)

        if self.writer_thread:
            self.writer_thread.join(timeout=timeout)
            if self.writer_thread.is_alive():
                raise RuntimeError("FFmpeg writer thread timeout")

        if not self.process:
            raise RuntimeError("FFmpeg process was not started")

        try:
            returncode = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            self.process.kill()
            raise RuntimeError("FFmpeg process timeout") from error

        if self.stderr_thread:
            self.stderr_thread.join(timeout=5)

        self.close_seconds = time.perf_counter() - started
        if self.write_error is not None:
            raise RuntimeError(f"FFmpeg writer failed: {self.write_error}")
        if returncode != 0:
            stderr_tail = "\n".join(self.stderr_lines[-40:])
            raise RuntimeError(f"FFmpeg pipe encoding failed:\n{stderr_tail}")

        return self.close_seconds

    def abort(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.kill()


def image_to_png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def frame_input_codec(capture_mode: str) -> str:
    return "jpeg" if capture_mode == "cdp-jpeg" else "png"


def frame_extension(capture_mode: str) -> str:
    return "jpg" if frame_input_codec(capture_mode) == "jpeg" else "png"


def image_to_frame_bytes(
    image: Image.Image,
    capture_mode: str,
    jpeg_quality: int,
) -> bytes:
    buffer = io.BytesIO()
    if frame_input_codec(capture_mode) == "jpeg":
        image.convert("RGB").save(buffer, format="JPEG", quality=jpeg_quality)
    else:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def image_looks_blank(image_bytes: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            gray = image.convert("L").resize((64, 64))
            low, high = gray.getextrema()
            return high - low < 3
    except Exception:
        return False


def capture_frame_bytes(
    page,
    cdp_session,
    capture_mode: str,
    capture_from_surface: bool,
    jpeg_quality: int,
) -> bytes:
    if capture_mode == "playwright-png":
        return page.screenshot(type="png", timeout=15000)

    params = {
        "format": "jpeg" if capture_mode == "cdp-jpeg" else "png",
        "captureBeyondViewport": False,
        "fromSurface": capture_from_surface,
        "optimizeForSpeed": True,
    }
    if capture_mode == "cdp-jpeg":
        params["quality"] = jpeg_quality

    try:
        result = cdp_session.send("Page.captureScreenshot", params)
    except Exception:
        params.pop("optimizeForSpeed", None)
        result = cdp_session.send("Page.captureScreenshot", params)
    return base64.b64decode(result["data"])


def write_optional_frame(path: Path, png_bytes: bytes, save_frames: bool) -> None:
    if save_frames:
        path.write_bytes(png_bytes)


def fit_photo_cover(photo: Image.Image, config: RenderConfig) -> Image.Image:
    return ImageOps.fit(
        photo.convert("RGB"),
        (config.width, config.height),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def cleanup_temp() -> None:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    temp_dir = TEMP_DIR.resolve()
    for item in TEMP_DIR.iterdir():
        if item.resolve().parent != temp_dir:
            raise RuntimeError(f"Unexpected temp path: {item}")
        if item.is_file():
            item.unlink()


def save_photo_frames(
    start_index: int,
    config: RenderConfig,
    last_map_frame: Path,
    photo_path: Path,
) -> int:
    map_image = Image.open(last_map_frame).convert("RGB")
    photo = Image.open(photo_path).convert("RGB")
    photo = ImageOps.fit(photo, (config.width, config.height), method=Image.Resampling.LANCZOS)
    fade_frames = max(1, round(config.fade_seconds * config.fps))
    hold_frames = max(1, round(config.photo_seconds * config.fps))
    black_frames = max(1, round(config.black_fade_seconds * config.fps))

    frame_index = start_index
    for i in range(fade_frames):
        alpha = (i + 1) / (fade_frames + 1)
        frame = Image.blend(map_image, photo, alpha)
        frame.save(FRAMES_DIR / f"frame_{frame_index:06d}.png")
        frame_index += 1

    for _ in range(hold_frames):
        photo.save(FRAMES_DIR / f"frame_{frame_index:06d}.png")
        frame_index += 1

    black = Image.new("RGB", (config.width, config.height), (0, 0, 0))
    for i in range(black_frames):
        alpha = (i + 1) / black_frames
        frame = Image.blend(photo, black, alpha)
        frame.save(FRAMES_DIR / f"frame_{frame_index:06d}.png")
        frame_index += 1

    return frame_index


def emit_photo_frames(
    start_index: int,
    config: RenderConfig,
    last_map_png: bytes,
    photo_path: Path,
    frame_writer: FfmpegPipeWriter,
    save_frames: bool,
    capture_mode: str,
    jpeg_quality: int,
    perf: PerfStats,
) -> int:
    started = time.perf_counter()
    map_image = Image.open(io.BytesIO(last_map_png)).convert("RGB")
    photo = Image.open(photo_path).convert("RGB")
    photo = ImageOps.fit(photo, (config.width, config.height), method=Image.Resampling.LANCZOS)
    fade_frames = max(1, round(config.fade_seconds * config.fps))
    hold_frames = max(1, round(config.photo_seconds * config.fps))
    black_frames = max(1, round(config.black_fade_seconds * config.fps))

    frame_index = start_index

    def emit(image: Image.Image) -> None:
        nonlocal frame_index
        frame_bytes = image_to_frame_bytes(image, capture_mode, jpeg_quality)
        output_started = time.perf_counter()
        write_optional_frame(
            FRAMES_DIR / f"frame_{frame_index:06d}.{frame_extension(capture_mode)}",
            frame_bytes,
            save_frames,
        )
        frame_writer.write_frame(frame_bytes)
        perf.add_stage("photo_frame_output", time.perf_counter() - output_started)
        frame_index += 1

    for i in range(fade_frames):
        alpha = (i + 1) / (fade_frames + 1)
        emit(Image.blend(map_image, photo, alpha))

    photo_bytes = image_to_frame_bytes(photo, capture_mode, jpeg_quality)
    for _ in range(hold_frames):
        output_started = time.perf_counter()
        write_optional_frame(
            FRAMES_DIR / f"frame_{frame_index:06d}.{frame_extension(capture_mode)}",
            photo_bytes,
            save_frames,
        )
        frame_writer.write_frame(photo_bytes)
        perf.add_stage("photo_frame_output", time.perf_counter() - output_started)
        frame_index += 1

    black = Image.new("RGB", (config.width, config.height), (0, 0, 0))
    for i in range(black_frames):
        alpha = (i + 1) / black_frames
        emit(Image.blend(photo, black, alpha))

    perf.add_stage("photo_fade_generation", time.perf_counter() - started)
    return frame_index


def emit_photo_segment_frames(
    start_index: int,
    config: RenderConfig,
    stop_map_png: bytes,
    photo_path: Path,
    frame_writer: FfmpegPipeWriter,
    save_frames: bool,
    capture_mode: str,
    jpeg_quality: int,
    perf: PerfStats,
    fade_in_seconds: float,
    hold_seconds: float,
    fade_out_seconds: float,
) -> int:
    if not photo_path.exists():
        print(f"[warn] photo file disappeared, skipped: {photo_path}")
        return start_index

    started = time.perf_counter()
    try:
        map_image = Image.open(io.BytesIO(stop_map_png)).convert("RGB")
        with Image.open(photo_path) as raw_photo:
            photo = fit_photo_cover(raw_photo, config)
    except Exception as error:
        print(f"[warn] failed to read photo, skipped: {photo_path}: {error}")
        return start_index

    fade_in_frames = frame_count_for_seconds(fade_in_seconds, config.fps)
    hold_frames = frame_count_for_seconds(hold_seconds, config.fps)
    fade_out_frames = frame_count_for_seconds(fade_out_seconds, config.fps)
    frame_index = start_index

    def emit(image: Image.Image) -> None:
        nonlocal frame_index
        frame_bytes = image_to_frame_bytes(image, capture_mode, jpeg_quality)
        output_started = time.perf_counter()
        write_optional_frame(
            FRAMES_DIR / f"frame_{frame_index:06d}.{frame_extension(capture_mode)}",
            frame_bytes,
            save_frames,
        )
        frame_writer.write_frame(frame_bytes)
        perf.add_stage("photo_frame_output", time.perf_counter() - output_started)
        frame_index += 1

    for i in range(fade_in_frames):
        alpha = (i + 1) / fade_in_frames
        emit(Image.blend(map_image, photo, alpha))

    photo_bytes = image_to_frame_bytes(photo, capture_mode, jpeg_quality)
    for _ in range(hold_frames):
        output_started = time.perf_counter()
        write_optional_frame(
            FRAMES_DIR / f"frame_{frame_index:06d}.{frame_extension(capture_mode)}",
            photo_bytes,
            save_frames,
        )
        frame_writer.write_frame(photo_bytes)
        perf.add_stage("photo_frame_output", time.perf_counter() - output_started)
        frame_index += 1

    for i in range(fade_out_frames):
        alpha = (i + 1) / fade_out_frames
        emit(Image.blend(photo, map_image, alpha))

    perf.add_stage("photo_fade_generation", time.perf_counter() - started)
    return frame_index


def require_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg 실행 파일을 찾을 수 없습니다. ffmpeg를 PATH에 추가하세요.")
    return ffmpeg


def resolve_bgm_path(
    cli_bgm: Path | None,
    raw_travel_data: dict[str, object],
    source_path: Path | None,
) -> Path | None:
    """Pick the BGM file: --bgm wins, else travel data's `bgm` field. None if absent."""
    candidate: Path | None = None
    if cli_bgm is not None:
        candidate = cli_bgm
    else:
        raw_bgm = raw_travel_data.get("bgm")
        if isinstance(raw_bgm, str) and raw_bgm.strip():
            candidate = Path(raw_bgm.strip())

    if candidate is None:
        return None

    resolved = resolve_project_path(candidate, source_path)
    if not resolved.exists():
        print(f"[warn] BGM 파일을 찾을 수 없어 무음으로 진행합니다: {candidate}")
        return None
    return resolved


def mux_bgm_into_video(
    ffmpeg: str,
    video_path: Path,
    bgm_path: Path,
    fade_out_seconds: float = 2.0,
) -> None:
    """Mux BGM into an already-rendered silent video (in place).

    Video is stream-copied (no re-encode); audio is looped to fill the video and
    cut to the video length with -shortest, with an optional fade-out tail.
    """
    duration = video_duration_seconds(video_path)

    audio_filters: list[str] = []
    if duration and fade_out_seconds > 0 and duration > fade_out_seconds:
        fade_start = max(0.0, duration - fade_out_seconds)
        audio_filters.append(f"afade=t=out:st={fade_start:.3f}:d={fade_out_seconds:.3f}")

    muxed_path = video_path.with_name(f"{video_path.stem}.bgm{video_path.suffix}")
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(video_path),
        "-stream_loop",
        "-1",
        "-i",
        str(bgm_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        "-movflags",
        "+faststart",
    ]
    if audio_filters:
        command += ["-af", ",".join(audio_filters)]
    command.append(str(muxed_path))

    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        muxed_path.unlink(missing_ok=True)
        stderr = result.stderr[-3000:] if result.stderr else ""
        raise RuntimeError(f"BGM 합성 실패:\n{stderr}")

    muxed_path.replace(video_path)


def video_duration_seconds(video_path: Path) -> float | None:
    metadata = ffprobe_metadata(video_path)
    raw_duration = metadata.get("duration")
    if raw_duration is None:
        return None
    try:
        return float(raw_duration)
    except (TypeError, ValueError):
        return None


def encode_video(ffmpeg: str, config: RenderConfig, output_path: Path) -> None:
    input_pattern = str(FRAMES_DIR / "frame_%06d.png")
    command = [
        ffmpeg,
        "-y",
        "-framerate",
        str(config.fps),
        "-start_number",
        "1",
        "-i",
        input_pattern,
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(config.fps),
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr[-3000:] if result.stderr else ""
        raise RuntimeError(f"FFmpeg 인코딩 실패:\n{stderr}")


def ffprobe_metadata(output_path: Path) -> dict[str, object]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}

    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,codec_name,pix_fmt,r_frame_rate,nb_frames,duration",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return {}

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}

    stream = (data.get("streams") or [{}])[0]
    metadata = {
        "width": stream.get("width"),
        "height": stream.get("height"),
        "codec": stream.get("codec_name"),
        "pixel_format": stream.get("pix_fmt"),
        "fps": stream.get("r_frame_rate"),
        "frame_count": stream.get("nb_frames"),
        "duration": stream.get("duration") or (data.get("format") or {}).get("duration"),
    }
    return {key: value for key, value in metadata.items() if value is not None}


def render_map_frames(
    token: str,
    config: RenderConfig,
    frame_writer: FfmpegPipeWriter,
    save_frames: bool,
    benchmark_frames: int | None,
    capture_mode: str,
    render_wait_mode: str,
    capture_from_surface: bool,
    jpeg_quality: int,
    warmup_enabled: bool,
    warmup_samples: int,
    warmup_timeout_ms: int,
    warmup_total_timeout_ms: int,
    perf: PerfStats,
    use_persistent_context: bool,
    gpu_mode: str = "auto",
    headed: bool = False,
    disable_software_rasterizer: bool = False,
    use_bearing_test_route: bool = False,
) -> tuple[int, bytes]:
    chromium_args = build_chromium_args(gpu_mode, disable_software_rasterizer)
    headless = not headed
    full_map_frame_count = max(2, round(config.map_seconds * config.fps))
    map_frame_count = full_map_frame_count
    if benchmark_frames is not None:
        map_frame_count = max(1, min(full_map_frame_count, benchmark_frames))
    debug_targets = {
        "bearing_before": 0.45,
        "bearing_turning": 0.50,
        "bearing_after": 0.55,
    }
    saved_debug_targets: set[str] = set()
    last_map_png = b""
    close_browser = None

    with sync_playwright() as playwright:
        launch_started = time.perf_counter()
        if use_persistent_context:
            try:
                CHROMIUM_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                context = playwright.chromium.launch_persistent_context(
                    str(CHROMIUM_PROFILE_DIR),
                    headless=headless,
                    viewport={"width": config.width, "height": config.height},
                    device_scale_factor=1,
                    args=chromium_args,
                )
                page = context.pages[0] if context.pages else context.new_page()
                close_browser = context.close
                print("[perf] chromium_mode=persistent")
            except Exception as error:
                print(f"[warn] Persistent Chromium context failed, fallback to regular launch: {error}")
                browser = playwright.chromium.launch(
                    headless=headless,
                    args=chromium_args,
                )
                context = browser.new_context(
                    viewport={"width": config.width, "height": config.height},
                    device_scale_factor=1,
                )
                page = context.new_page()
                close_browser = browser.close
                print("[perf] chromium_mode=regular")
        else:
            browser = playwright.chromium.launch(
                headless=headless,
                args=chromium_args,
            )
            context = browser.new_context(
                viewport={"width": config.width, "height": config.height},
                device_scale_factor=1,
            )
            page = context.new_page()
            close_browser = browser.close
            print("[perf] chromium_mode=regular")
        perf.add_stage("chromium_start", time.perf_counter() - launch_started)
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Page.enable")

        warning_counts: dict[str, int] = {}

        def on_console(msg) -> None:
            text = msg.text
            if msg.type == "warning":
                warning_key = None
                if "WebGL:" in text or "GL_INVALID" in text or "GL Driver Message" in text:
                    warning_key = "webgl"
                elif "Warmup idle timeout" in text:
                    warning_key = "warmup-timeout"
                if warning_key:
                    warning_counts[warning_key] = warning_counts.get(warning_key, 0) + 1
                    if warning_counts[warning_key] > 3:
                        return
            if msg.type in {"error", "warning"} or text.startswith("[bearing]"):
                print(f"[browser:{msg.type}] {text}")

        page.on("console", on_console)
        page.on("pageerror", lambda error: print(f"[pageerror] {error}"))
        page.add_init_script(
            "window.MAPBOX_ACCESS_TOKEN = "
            f"{json.dumps(token)};"
            f"window.RENDER_FPS = {config.fps};"
            f"window.RENDER_MODE = {json.dumps(config.mode_name)};"
            "window.DEBUG_BEARING = true;"
            "window.DEBUG_RENDER_TIMEOUTS = false;"
            "window.FRAME_RENDER_TIMEOUT_MS = 1200;"
            "window.RENDER_RAF_COUNT = 1;"
            f"window.WARMUP_IDLE_TIMEOUT_MS = {warmup_timeout_ms};"
            f"window.WARMUP_TOTAL_TIMEOUT_MS = {warmup_total_timeout_ms};"
            f"window.USE_BEARING_TEST_ROUTE = {json.dumps(use_bearing_test_route)};"
        )
        goto_started = time.perf_counter()
        page.goto(HTML_PATH.as_uri(), wait_until="domcontentloaded", timeout=60000)
        perf.add_stage("html_load", time.perf_counter() - goto_started)
        page.wait_for_function("typeof window.initializeMap === 'function'", timeout=30000)

        init_started = time.perf_counter()
        init_result = page.evaluate("window.initializeMap()")
        perf.add_stage("mapbox_initialization", time.perf_counter() - init_started)
        if isinstance(init_result, dict):
            initial_idle_ms = float(init_result.get("initialIdleMs") or 0.0)
            perf.add_stage("initial_map_idle", initial_idle_ms / 1000.0)
            print(
                "[perf] mapbox_init "
                f"initial_idle={initial_idle_ms:.1f}ms "
                f"route_points={init_result.get('routeCoordinateCount', 'n/a')}"
            )
        page.wait_for_function("window.isRenderReady && window.isRenderReady()", timeout=90000)
        webgl_info = page.evaluate("window.getWebGLInfo && window.getWebGLInfo()")
        if isinstance(webgl_info, dict):
            renderer = str(webgl_info.get("renderer", "unknown"))
            print(f"[webgl] vendor={webgl_info.get('vendor', 'unknown')}")
            print(f"[webgl] renderer={renderer}")
            print(f"[webgl] version={webgl_info.get('version', 'unknown')}")
            print(f"[webgl] devicePixelRatio={webgl_info.get('devicePixelRatio', 'unknown')}")
            print(
                "[webgl] canvas="
                f"{webgl_info.get('canvasWidth', 'unknown')}x{webgl_info.get('canvasHeight', 'unknown')} "
                f"client={webgl_info.get('canvasClientWidth', 'unknown')}x{webgl_info.get('canvasClientHeight', 'unknown')} "
                f"viewport={webgl_info.get('viewportWidth', 'unknown')}x{webgl_info.get('viewportHeight', 'unknown')}"
            )
            if is_software_renderer(renderer):
                print("=" * 72)
                print(f"[warn] WebGL is using SOFTWARE rendering: {renderer}")
                print("[warn] GPU 가속이 비활성화되어 ReadPixels/screenshot 병목이 큽니다.")
                print("[warn] 5분 목표 위험: 1080x1920 30fps 풀 렌더가 매우 느릴 수 있습니다.")
                print("[warn] 해결: --gpu-mode auto (기본, --use-angle=d3d11) 사용,")
                print("[warn]       또는 --headed 로 실행해 GPU 백엔드를 강제하세요.")
                print("=" * 72)
            else:
                print(f"[webgl] GPU 가속 활성화됨: {renderer}")

        if warmup_enabled:
            warmup_started = time.perf_counter()
            try:
                warmup_result = page.evaluate(
                    "(samples) => window.warmUpRouteTiles(samples)",
                    warmup_samples,
                )
                perf.add_stage("tile_warmup", time.perf_counter() - warmup_started)
                if isinstance(warmup_result, dict):
                    print(
                        "[perf] tile_warmup "
                        f"samples={warmup_result.get('samples')} "
                        f"timeouts={warmup_result.get('timeoutCount')} "
                        f"total_timeout={warmup_result.get('totalTimeoutHit')} "
                        f"js_ms={float(warmup_result.get('totalMs') or 0.0):.1f}"
                    )
            except Exception as error:
                perf.add_stage("tile_warmup", time.perf_counter() - warmup_started)
                print(f"[warn] Tile warmup failed; continuing render: {error}")
        else:
            print("[perf] tile_warmup skipped")

        ffmpeg_started = time.perf_counter()
        frame_writer.start()
        perf.add_stage("ffmpeg_start", time.perf_counter() - ffmpeg_started)
        active_capture_from_surface = capture_from_surface

        for index in range(1, map_frame_count + 1):
            progress = (index - 1) / (full_map_frame_count - 1)
            frame_started = time.perf_counter()
            try:
                render_started = time.perf_counter()
                frame_info = page.evaluate(
                    "([progress, waitMode]) => window.renderFrame(progress, waitMode)",
                    [progress, render_wait_mode],
                )
                render_call_ms = (time.perf_counter() - render_started) * 1000.0
                render_wait_ms = 0.0
                if isinstance(frame_info, dict):
                    render_wait_ms = float(frame_info.get("renderWaitMs") or 0.0)

                screenshot_started = time.perf_counter()
                frame_bytes = capture_frame_bytes(
                    page,
                    cdp_session,
                    capture_mode,
                    active_capture_from_surface,
                    jpeg_quality,
                )
                if index == 1 and capture_mode.startswith("cdp-") and not active_capture_from_surface:
                    if image_looks_blank(frame_bytes):
                        print("[warn] capture_from_surface=false returned blank frame; retrying with true")
                        active_capture_from_surface = True
                        frame_bytes = capture_frame_bytes(
                            page,
                            cdp_session,
                            capture_mode,
                            active_capture_from_surface,
                            jpeg_quality,
                        )
                screenshot_ms = (time.perf_counter() - screenshot_started) * 1000.0
                last_map_png = frame_bytes

                output_started = time.perf_counter()
                frame_path = FRAMES_DIR / f"frame_{index:06d}.{frame_extension(capture_mode)}"
                write_optional_frame(frame_path, frame_bytes, save_frames)
                for name, target_progress in debug_targets.items():
                    if name not in saved_debug_targets and progress >= target_progress:
                        (DEBUG_DIR / f"{name}.{frame_extension(capture_mode)}").write_bytes(frame_bytes)
                        saved_debug_targets.add(name)
                frame_writer.write_frame(frame_bytes)
                output_ms = (time.perf_counter() - output_started) * 1000.0
                total_ms = (time.perf_counter() - frame_started) * 1000.0
                perf.add_frame(total_ms, render_call_ms, render_wait_ms, screenshot_ms, output_ms)
                if index == 1 or index % 15 == 0 or index == map_frame_count:
                    print(
                        "[perf:frame] "
                        f"{index:06d}/{map_frame_count:06d} "
                        f"render={render_call_ms:.1f}ms "
                        f"js_wait={render_wait_ms:.1f}ms "
                        f"screenshot={screenshot_ms:.1f}ms "
                        f"output={output_ms:.1f}ms "
                        f"total={total_ms:.1f}ms"
                    )
            except Exception as error:
                print(f"프레임 {index:06d} 렌더링 실패: {error}")
                raise

        if close_browser is not None:
            close_started = time.perf_counter()
            close_browser()
            perf.add_stage("chromium_close", time.perf_counter() - close_started)

    if not last_map_png:
        raise RuntimeError("No map frames were captured")
    return map_frame_count, last_map_png


def render_timeline_frames(
    token: str,
    config: RenderConfig,
    frame_writer: FfmpegPipeWriter,
    save_frames: bool,
    benchmark_frames: int | None,
    capture_mode: str,
    render_wait_mode: str,
    capture_from_surface: bool,
    jpeg_quality: int,
    warmup_enabled: bool,
    warmup_samples: int,
    warmup_timeout_ms: int,
    warmup_total_timeout_ms: int,
    perf: PerfStats,
    use_persistent_context: bool,
    use_bearing_test_route: bool,
    browser_travel_data: dict[str, object],
    timeline_segments: list[TimelineSegment],
    gpu_mode: str = "auto",
    headed: bool = False,
    disable_software_rasterizer: bool = False,
) -> tuple[int, bytes]:
    chromium_args = build_chromium_args(gpu_mode, disable_software_rasterizer)
    headless = not headed
    total_timeline_frames = timeline_total_frames(timeline_segments, config)
    target_frames = (
        min(benchmark_frames, total_timeline_frames)
        if benchmark_frames is not None
        else total_timeline_frames
    )
    debug_targets = {
        "bearing_before": 0.45,
        "bearing_turning": 0.50,
        "bearing_after": 0.55,
    }
    saved_debug_targets: set[str] = set()
    last_map_png = b""
    last_stop_map_png = b""
    close_browser = None

    with sync_playwright() as playwright:
        launch_started = time.perf_counter()
        if use_persistent_context:
            try:
                CHROMIUM_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                context = playwright.chromium.launch_persistent_context(
                    str(CHROMIUM_PROFILE_DIR),
                    headless=headless,
                    viewport={"width": config.width, "height": config.height},
                    device_scale_factor=1,
                    args=chromium_args,
                )
                page = context.pages[0] if context.pages else context.new_page()
                close_browser = context.close
                print("[perf] chromium_mode=persistent")
            except Exception as error:
                print(f"[warn] Persistent Chromium context failed, fallback to regular launch: {error}")
                browser = playwright.chromium.launch(
                    headless=headless,
                    args=chromium_args,
                )
                context = browser.new_context(
                    viewport={"width": config.width, "height": config.height},
                    device_scale_factor=1,
                )
                page = context.new_page()
                close_browser = browser.close
                print("[perf] chromium_mode=regular")
        else:
            browser = playwright.chromium.launch(
                headless=headless,
                args=chromium_args,
            )
            context = browser.new_context(
                viewport={"width": config.width, "height": config.height},
                device_scale_factor=1,
            )
            page = context.new_page()
            close_browser = browser.close
            print("[perf] chromium_mode=regular")
        perf.add_stage("chromium_start", time.perf_counter() - launch_started)
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Page.enable")

        warning_counts: dict[str, int] = {}

        def on_console(msg) -> None:
            text = msg.text
            if msg.type == "warning":
                warning_key = None
                if "WebGL:" in text or "GL_INVALID" in text or "GL Driver Message" in text:
                    warning_key = "webgl"
                elif "Warmup idle timeout" in text:
                    warning_key = "warmup-timeout"
                if warning_key:
                    warning_counts[warning_key] = warning_counts.get(warning_key, 0) + 1
                    if warning_counts[warning_key] > 3:
                        return
            if msg.type in {"error", "warning"} or text.startswith("[bearing]"):
                print(f"[browser:{msg.type}] {text}")

        page.on("console", on_console)
        page.on("pageerror", lambda error: print(f"[pageerror] {error}"))
        page.add_init_script(
            "window.MAPBOX_ACCESS_TOKEN = "
            f"{json.dumps(token)};"
            f"window.TRAVEL_DATA = {json.dumps(browser_travel_data, ensure_ascii=False)};"
            f"window.RENDER_FPS = {config.fps};"
            f"window.RENDER_MODE = {json.dumps(config.mode_name)};"
            "window.DEBUG_BEARING = true;"
            "window.DEBUG_RENDER_TIMEOUTS = false;"
            "window.FRAME_RENDER_TIMEOUT_MS = 1200;"
            "window.RENDER_RAF_COUNT = 1;"
            f"window.WARMUP_IDLE_TIMEOUT_MS = {warmup_timeout_ms};"
            f"window.WARMUP_TOTAL_TIMEOUT_MS = {warmup_total_timeout_ms};"
            f"window.USE_BEARING_TEST_ROUTE = {json.dumps(use_bearing_test_route)};"
        )
        goto_started = time.perf_counter()
        page.goto(HTML_PATH.as_uri(), wait_until="domcontentloaded", timeout=60000)
        perf.add_stage("html_load", time.perf_counter() - goto_started)
        page.wait_for_function("typeof window.initializeMap === 'function'", timeout=30000)

        init_started = time.perf_counter()
        init_result = page.evaluate("window.initializeMap()")
        perf.add_stage("mapbox_initialization", time.perf_counter() - init_started)
        if isinstance(init_result, dict):
            initial_idle_ms = float(init_result.get("initialIdleMs") or 0.0)
            perf.add_stage("initial_map_idle", initial_idle_ms / 1000.0)
            print(
                "[perf] mapbox_init "
                f"initial_idle={initial_idle_ms:.1f}ms "
                f"route_points={init_result.get('routeCoordinateCount', 'n/a')} "
                f"track_points={init_result.get('trackPointCount', 'n/a')} "
                f"media_points={init_result.get('mediaPointCount', 'n/a')}"
            )
        page.wait_for_function("window.isRenderReady && window.isRenderReady()", timeout=90000)
        webgl_info = page.evaluate("window.getWebGLInfo && window.getWebGLInfo()")
        if isinstance(webgl_info, dict):
            renderer = str(webgl_info.get("renderer", "unknown"))
            print(f"[webgl] vendor={webgl_info.get('vendor', 'unknown')}")
            print(f"[webgl] renderer={renderer}")
            print(f"[webgl] version={webgl_info.get('version', 'unknown')}")
            print(f"[webgl] devicePixelRatio={webgl_info.get('devicePixelRatio', 'unknown')}")
            print(
                "[webgl] canvas="
                f"{webgl_info.get('canvasWidth', 'unknown')}x{webgl_info.get('canvasHeight', 'unknown')} "
                f"client={webgl_info.get('canvasClientWidth', 'unknown')}x{webgl_info.get('canvasClientHeight', 'unknown')} "
                f"viewport={webgl_info.get('viewportWidth', 'unknown')}x{webgl_info.get('viewportHeight', 'unknown')}"
            )
            if is_software_renderer(renderer):
                print("=" * 72)
                print(f"[warn] WebGL is using SOFTWARE rendering: {renderer}")
                print("[warn] GPU 가속이 비활성화되어 ReadPixels/screenshot 병목이 큽니다.")
                print("[warn] 5분 목표 위험: 1080x1920 30fps 풀 렌더가 매우 느릴 수 있습니다.")
                print("[warn] 해결: --gpu-mode auto (기본, --use-angle=d3d11) 사용,")
                print("[warn]       또는 --headed 로 실행해 GPU 백엔드를 강제하세요.")
                print("=" * 72)
            else:
                print(f"[webgl] GPU 가속 활성화됨: {renderer}")

        if warmup_enabled:
            warmup_started = time.perf_counter()
            try:
                warmup_result = page.evaluate(
                    "(samples) => window.warmUpRouteTiles(samples)",
                    warmup_samples,
                )
                perf.add_stage("tile_warmup", time.perf_counter() - warmup_started)
                if isinstance(warmup_result, dict):
                    print(
                        "[perf] tile_warmup "
                        f"samples={warmup_result.get('samples')} "
                        f"timeouts={warmup_result.get('timeoutCount')} "
                        f"total_timeout={warmup_result.get('totalTimeoutHit')} "
                        f"js_ms={float(warmup_result.get('totalMs') or 0.0):.1f}"
                    )
            except Exception as error:
                perf.add_stage("tile_warmup", time.perf_counter() - warmup_started)
                print(f"[warn] Tile warmup failed; continuing render: {error}")
        else:
            print("[perf] tile_warmup skipped")

        ffmpeg_started = time.perf_counter()
        frame_writer.start()
        perf.add_stage("ffmpeg_start", time.perf_counter() - ffmpeg_started)
        active_capture_from_surface = capture_from_surface
        frame_index = 1

        def benchmark_done() -> bool:
            return benchmark_frames is not None and frame_index > benchmark_frames

        def capture_and_write_map_frame(
            frame_started: float,
            render_call_ms: float,
            render_wait_ms: float,
        ) -> bytes:
            nonlocal active_capture_from_surface, frame_index, last_map_png

            screenshot_started = time.perf_counter()
            frame_bytes = capture_frame_bytes(
                page,
                cdp_session,
                capture_mode,
                active_capture_from_surface,
                jpeg_quality,
            )
            if frame_index == 1 and capture_mode.startswith("cdp-") and not active_capture_from_surface:
                if image_looks_blank(frame_bytes):
                    print("[warn] capture_from_surface=false returned blank frame; retrying with true")
                    active_capture_from_surface = True
                    frame_bytes = capture_frame_bytes(
                        page,
                        cdp_session,
                        capture_mode,
                        active_capture_from_surface,
                        jpeg_quality,
                    )
            screenshot_ms = (time.perf_counter() - screenshot_started) * 1000.0
            last_map_png = frame_bytes

            output_started = time.perf_counter()
            frame_path = FRAMES_DIR / f"frame_{frame_index:06d}.{frame_extension(capture_mode)}"
            write_optional_frame(frame_path, frame_bytes, save_frames)
            for name, target_progress in debug_targets.items():
                if name not in saved_debug_targets and target_frames > 0:
                    if frame_index / target_frames >= target_progress:
                        (DEBUG_DIR / f"{name}.{frame_extension(capture_mode)}").write_bytes(frame_bytes)
                        saved_debug_targets.add(name)
            frame_writer.write_frame(frame_bytes)
            output_ms = (time.perf_counter() - output_started) * 1000.0
            total_ms = (time.perf_counter() - frame_started) * 1000.0
            perf.add_frame(total_ms, render_call_ms, render_wait_ms, screenshot_ms, output_ms)

            if frame_index == 1 or frame_index % 15 == 0 or frame_index == target_frames:
                print(
                    "[perf:frame] "
                    f"{frame_index:06d}/{target_frames:06d} "
                    f"render={render_call_ms:.1f}ms "
                    f"js_wait={render_wait_ms:.1f}ms "
                    f"screenshot={screenshot_ms:.1f}ms "
                    f"output={output_ms:.1f}ms "
                    f"total={total_ms:.1f}ms"
                )

            frame_index += 1
            return frame_bytes

        for segment_index, segment in enumerate(timeline_segments, start=1):
            if benchmark_done():
                break

            print(
                "[timeline] "
                f"{segment_index}/{len(timeline_segments)} type={segment.type} "
                f"duration={segment.duration:.2f}s"
            )

            if segment.type == "map_move":
                frame_count = frame_count_for_seconds(segment.duration, config.fps)
                for local_index in range(frame_count):
                    if benchmark_done():
                        break
                    progress = (
                        local_index / (frame_count - 1)
                        if frame_count > 1
                        else 1.0
                    )
                    frame_started = time.perf_counter()
                    try:
                        render_started = time.perf_counter()
                        frame_info = page.evaluate(
                            "([startIndex, endIndex, progress, waitMode]) => "
                            "window.renderRouteSegment(startIndex, endIndex, progress, waitMode)",
                            [
                                segment.start_track_index,
                                segment.end_track_index,
                                progress,
                                render_wait_mode,
                            ],
                        )
                        render_call_ms = (time.perf_counter() - render_started) * 1000.0
                        render_wait_ms = (
                            float(frame_info.get("renderWaitMs") or 0.0)
                            if isinstance(frame_info, dict)
                            else 0.0
                        )
                        capture_and_write_map_frame(
                            frame_started,
                            render_call_ms,
                            render_wait_ms,
                        )
                    except Exception as error:
                        print(f"프레임 {frame_index:06d} 렌더링 실패: {error}")
                        raise
                continue

            if segment.type == "map_hold":
                frame_count = frame_count_for_seconds(segment.duration, config.fps)
                for _ in range(frame_count):
                    if benchmark_done():
                        break
                    frame_started = time.perf_counter()
                    try:
                        render_started = time.perf_counter()
                        frame_info = page.evaluate(
                            "([trackIndex, name, waitMode]) => "
                            "window.renderStopPoint(trackIndex, name, waitMode)",
                            [segment.track_index, segment.name or "", render_wait_mode],
                        )
                        render_call_ms = (time.perf_counter() - render_started) * 1000.0
                        render_wait_ms = (
                            float(frame_info.get("renderWaitMs") or 0.0)
                            if isinstance(frame_info, dict)
                            else 0.0
                        )
                        last_stop_map_png = capture_and_write_map_frame(
                            frame_started,
                            render_call_ms,
                            render_wait_ms,
                        )
                    except Exception as error:
                        print(f"프레임 {frame_index:06d} 렌더링 실패: {error}")
                        raise
                continue

            if segment.type == "photo":
                if benchmark_frames is not None:
                    continue
                if not last_stop_map_png:
                    frame_started = time.perf_counter()
                    render_started = time.perf_counter()
                    frame_info = page.evaluate(
                        "([trackIndex, name, waitMode]) => "
                        "window.renderStopPoint(trackIndex, name, waitMode)",
                        [segment.track_index, segment.name or "", render_wait_mode],
                    )
                    render_call_ms = (time.perf_counter() - render_started) * 1000.0
                    render_wait_ms = (
                        float(frame_info.get("renderWaitMs") or 0.0)
                        if isinstance(frame_info, dict)
                        else 0.0
                    )
                    last_stop_map_png = capture_and_write_map_frame(
                        frame_started,
                        render_call_ms,
                        render_wait_ms,
                    )
                if segment.photo_path is None:
                    continue
                frame_index = emit_photo_segment_frames(
                    start_index=frame_index,
                    config=config,
                    stop_map_png=last_stop_map_png,
                    photo_path=segment.photo_path,
                    frame_writer=frame_writer,
                    save_frames=save_frames,
                    capture_mode=capture_mode,
                    jpeg_quality=jpeg_quality,
                    perf=perf,
                    fade_in_seconds=segment.fade_in_seconds,
                    hold_seconds=segment.hold_seconds,
                    fade_out_seconds=segment.fade_out_seconds,
                )
                last_map_png = last_stop_map_png
                continue

            print(f"[warn] unknown timeline segment type skipped: {segment.type}")

        if close_browser is not None:
            close_started = time.perf_counter()
            close_browser()
            perf.add_stage("chromium_close", time.perf_counter() - close_started)

    if not last_map_png:
        raise RuntimeError("No frames were captured")
    return frame_index - 1, last_map_png


def print_metadata(
    output_path: Path,
    output_id: int,
    frame_count: int,
    config: RenderConfig,
    probe: dict[str, object],
) -> None:
    duration = frame_count / config.fps
    print("렌더링 완료")
    print(f"출력 파일: {output_path}")
    print(f"출력 ID: {output_id}")
    print(f"프레임 수: {probe.get('frame_count', frame_count)}")
    print(f"해상도: {probe.get('width', config.width)}x{probe.get('height', config.height)}")
    print(f"FPS: {probe.get('fps', f'{config.fps}/1')}")
    print(f"영상 길이: {float(probe.get('duration', duration)):.2f}초")
    print(f"코덱: {probe.get('codec', 'unknown')}")
    print(f"픽셀 포맷: {probe.get('pixel_format', 'unknown')}")


def average_ms(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def print_benchmark_summary(
    config: RenderConfig,
    perf: PerfStats,
    capture_mode: str,
    render_wait_mode: str,
    frame_count: int,
    output_path: Path,
    full_frame_count: int,
) -> None:
    avg_total_ms = average_ms(perf.frame_total_ms)
    avg_wait_ms = average_ms(perf.frame_render_wait_ms)
    avg_capture_ms = average_ms(perf.frame_screenshot_ms)
    measured_frame_seconds = sum(perf.frame_total_ms) / 1000.0
    elapsed = time.perf_counter() - perf.started
    non_frame_seconds = max(0.0, elapsed - measured_frame_seconds)
    estimated_full = non_frame_seconds + (avg_total_ms / 1000.0) * full_frame_count
    file_size = output_path.stat().st_size if output_path.exists() else 0

    print("[benchmark]")
    print(f"[benchmark] capture_mode={capture_mode}")
    print(f"[benchmark] render_wait_mode={render_wait_mode}")
    print(f"[benchmark] frames={frame_count}")
    print(f"[benchmark] render_wait_avg={avg_wait_ms:.1f}ms")
    print(f"[benchmark] capture_avg={avg_capture_ms:.1f}ms")
    print(f"[benchmark] frame_total_avg={avg_total_ms:.1f}ms")
    print(f"[benchmark] estimated_full_render={estimated_full:.1f}s")
    print(f"[benchmark] output_size={file_size} bytes")


def run() -> int:
    perf = PerfStats()
    args = parse_args()
    config = build_config(args)
    token = load_token()
    if token is None:
        print(TOKEN_MISSING_MESSAGE)
        return 1

    ensure_directories()
    ensure_destination_photo()
    raw_travel_data, travel_data_path, travel_data_label = load_raw_travel_data(
        args.travel_data,
        args.bearing_test_route,
    )
    track_points, media_points = validate_travel_data(raw_travel_data, travel_data_path)
    bgm_path = resolve_bgm_path(args.bgm, raw_travel_data, travel_data_path)
    browser_travel_data = travel_data_for_browser(track_points, media_points)
    timeline_segments = build_timeline_segments(track_points, media_points, config)
    full_timeline_frames = timeline_total_frames(timeline_segments, config)
    output_path, output_id = next_output_path()
    ffmpeg = require_ffmpeg()
    clear_previous_frames()
    clear_debug_images()
    frame_writer = FfmpegPipeWriter(
        ffmpeg=ffmpeg,
        config=config,
        output_path=output_path,
        preset=args.x264_preset,
        crf=args.crf,
        input_codec=frame_input_codec(args.capture_mode),
        queue_size=max(1, args.queue_size),
    )

    print(f"렌더링 모드: {config.mode_name}")
    print(f"해상도: {config.width}x{config.height}, FPS: {config.fps}")
    print(f"프레임 저장: {'yes' if args.save_frames else 'no'}")
    print(f"capture_mode={args.capture_mode}")
    print(f"render_wait_mode={args.render_wait_mode}")
    print(f"gpu_mode={args.gpu_mode} headed={'yes' if args.headed else 'no'}")
    print(f"capture_from_surface={args.capture_from_surface}")
    print(f"warmup={'yes' if args.warmup else 'no'}")
    if args.benchmark_frames:
        print(f"benchmark_frames={args.benchmark_frames}")
    print(f"travel_data={travel_data_label}")
    print(f"track_points={len(track_points)}, media_points={len(media_points)}")
    print(
        "photo_points="
        f"{sum(1 for point in media_points if point.photos)}, "
        f"photos={sum(len(point.photos) for point in media_points)}"
    )
    print_timeline_summary(timeline_segments, config)
    print(f"FFmpeg pipe: yes, preset={args.x264_preset}, crf={args.crf}")
    print(f"BGM: {bgm_path.name if bgm_path else 'none'}")
    print("Mapbox token loaded: yes")
    print(f"출력 예정 파일: {output_path}")

    try:
        frame_count, _last_map_png = render_timeline_frames(
            token=token,
            config=config,
            frame_writer=frame_writer,
            save_frames=args.save_frames,
            benchmark_frames=args.benchmark_frames,
            capture_mode=args.capture_mode,
            render_wait_mode=args.render_wait_mode,
            capture_from_surface=args.capture_from_surface,
            jpeg_quality=args.jpeg_quality,
            warmup_enabled=args.warmup,
            warmup_samples=args.warmup_samples,
            warmup_timeout_ms=args.warmup_timeout_ms,
            warmup_total_timeout_ms=args.warmup_total_timeout_ms,
            perf=perf,
            use_persistent_context=args.use_persistent_context,
            use_bearing_test_route=args.bearing_test_route,
            browser_travel_data=browser_travel_data,
            timeline_segments=timeline_segments,
            gpu_mode=args.gpu_mode,
            headed=args.headed,
            disable_software_rasterizer=args.disable_software_rasterizer,
        )
        close_seconds = frame_writer.close()
        perf.add_stage("ffmpeg_encode_and_finalize", close_seconds)
        if bgm_path is not None and not args.benchmark_frames:
            bgm_started = time.perf_counter()
            mux_bgm_into_video(ffmpeg, output_path, bgm_path)
            perf.add_stage("bgm_mux", time.perf_counter() - bgm_started)
            print(f"BGM 합성 완료: {bgm_path.name}")
        cleanup_temp()
        probe = ffprobe_metadata(output_path)
        print_metadata(output_path, output_id, frame_count, config, probe)
        if args.benchmark_frames:
            print_benchmark_summary(
                config=config,
                perf=perf,
                capture_mode=args.capture_mode,
                render_wait_mode=args.render_wait_mode,
                frame_count=frame_count,
                output_path=output_path,
                full_frame_count=full_timeline_frames,
            )
        perf.print_report()
        return 0
    except Exception:
        frame_writer.abort()
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except PlaywrightTimeoutError as error:
        print(f"Playwright timeout: {error}")
        raise SystemExit(1)
    except Exception as error:
        print(f"실행 실패: {error}")
        raise SystemExit(1)
