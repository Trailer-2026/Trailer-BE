# -*- coding: utf-8 -*-
"""
Builder server for the video3D travel-route renderer.

Serves an HTML page where you can:
  - click a Mapbox map (or type coordinates) to add as many GPS points as you want,
  - attach photos to each point,
  - pick a BGM track from the bgm/ folder,
and then kicks off render_video.py to produce an MP4 with that BGM muxed in.

Run (inside the trailer3d conda env, from this directory):
    conda run -n trailer3d python -m uvicorn server:app --reload --port 8200
or
    conda run -n trailer3d python server.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

ROOT = Path(__file__).resolve().parent
BGM_DIR = ROOT / "bgm"
ASSETS_DIR = ROOT / "assets"
UPLOADS_DIR = ASSETS_DIR / "uploads"
OUTPUT_DIR = ROOT / "output"
BUILDER_HTML = ROOT / "builder.html"
RENDER_SCRIPT = ROOT / "render_video.py"
MODAL_SCRIPT = ROOT / "modal_render.py"

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
RENDER_TIMEOUT_SECONDS = 60 * 30  # 30 min hard cap
# render_video.py --theme 와 map_themes.js THEMES 에 맞춰 유지.
ALLOWED_THEMES = {"default", "spring", "summer", "autumn", "winter"}
# Standard 스타일 시간대 조명 (빈 값 = 테마 기본).
ALLOWED_LIGHT_PRESETS = {"", "dawn", "day", "dusk", "night"}

load_dotenv(ROOT / ".env")

app = FastAPI(title="video3D builder")


# --------------------------------------------------------------------------- #
# BGM helpers
# --------------------------------------------------------------------------- #
def bgm_display_name(filename: str) -> dict[str, str]:
    """Turn a (possibly messy Pixabay-attribution) filename into title/artist."""
    stem = Path(filename).stem
    # Pixabay export filenames look like:
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
            info = bgm_display_name(path.name)
            tracks.append({"file": path.name, **info})
    return tracks


def safe_bgm_path(filename: str) -> Path:
    """Resolve a BGM filename to a path guaranteed to live inside BGM_DIR."""
    candidate = (BGM_DIR / filename).resolve()
    if candidate.parent != BGM_DIR.resolve() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="BGM not found")
    if candidate.suffix.lower() not in AUDIO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Not an audio file")
    return candidate


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    if not BUILDER_HTML.exists():
        raise HTTPException(status_code=500, detail="builder.html missing")
    html = BUILDER_HTML.read_text(encoding="utf-8")
    token = os.environ.get("MAPBOX_ACCESS_TOKEN", "")
    html = html.replace("__MAPBOX_TOKEN__", token)
    return HTMLResponse(html)


@app.get("/map_themes.js")
async def serve_map_themes() -> FileResponse:
    """빌더 미리보기가 렌더러와 같은 테마 모듈을 공유한다."""
    path = ROOT / "map_themes.js"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="map_themes.js missing")
    return FileResponse(path, media_type="application/javascript")


@app.get("/api/bgm")
async def api_bgm() -> JSONResponse:
    return JSONResponse(list_bgm())


@app.get("/api/bgm/preview")
async def api_bgm_preview(file: str) -> FileResponse:
    path = safe_bgm_path(file)
    return FileResponse(path, media_type="audio/mpeg")


@app.get("/output/{name}")
async def serve_output(name: str) -> FileResponse:
    candidate = (OUTPUT_DIR / name).resolve()
    if candidate.parent != OUTPUT_DIR.resolve() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Output not found")
    return FileResponse(candidate, media_type="video/mp4", filename=name)


@app.post("/api/render")
async def api_render(
    points: str = Form(...),
    bgm: str = Form(""),
    quick: str = Form("false"),
    engine: str = Form("local"),
    theme: str = Form("default"),
    light_preset: str = Form(""),
    intro: str = Form("false"),
    photo_points: str = Form("[]"),
    photos: list[UploadFile] | None = None,
) -> JSONResponse:
    try:
        raw_points = json.loads(points)
        photo_owner_indices = json.loads(photo_points)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {error}") from error

    if not isinstance(raw_points, list) or len(raw_points) < 2:
        raise HTTPException(status_code=400, detail="GPS 지점은 최소 2개가 필요합니다.")

    photos = photos or []
    if len(photo_owner_indices) != len(photos):
        raise HTTPException(status_code=400, detail="photo_points와 photos 개수가 다릅니다.")

    job_id = uuid.uuid4().hex[:12]
    job_dir = UPLOADS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Save uploaded photos, grouped by the point they belong to.
    photos_by_point: dict[int, list[str]] = {}
    for order, (upload, owner) in enumerate(zip(photos, photo_owner_indices)):
        try:
            point_index = int(owner)
        except (TypeError, ValueError):
            continue
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            suffix = ".jpg"
        saved = job_dir / f"p{point_index}_{order}{suffix}"
        saved.write_bytes(await upload.read())
        rel = saved.relative_to(ROOT).as_posix()
        photos_by_point.setdefault(point_index, []).append(rel)

    # Build travel_data.json (trackPoints + mediaPoints).
    track_points: list[dict[str, object]] = []
    media_points: list[dict[str, object]] = []
    for index, point in enumerate(raw_points):
        try:
            latitude = float(point["latitude"])
            longitude = float(point["longitude"])
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(
                status_code=400, detail=f"지점 {index}의 좌표가 올바르지 않습니다."
            ) from error
        track_points.append({"latitude": latitude, "longitude": longitude})
        name = str(point.get("name") or f"지점 {index + 1}").strip()
        media_points.append(
            {
                "trackIndex": index,
                "name": name,
                "photos": photos_by_point.get(index, []),
            }
        )

    travel_data: dict[str, object] = {
        "trackPoints": track_points,
        "mediaPoints": media_points,
    }
    bgm_path: Path | None = None
    if bgm.strip():
        bgm_path = safe_bgm_path(bgm.strip())
        travel_data["bgm"] = bgm_path.relative_to(ROOT).as_posix()

    travel_data_path = job_dir / "travel_data.json"
    travel_data_path.write_text(
        json.dumps(travel_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    is_quick = quick.lower() == "true"
    engine = engine.lower().strip()
    theme = (theme or "default").lower().strip()
    if theme not in ALLOWED_THEMES:
        raise HTTPException(status_code=400, detail=f"알 수 없는 테마: {theme}")
    light_preset = (light_preset or "").lower().strip()
    if light_preset not in ALLOWED_LIGHT_PRESETS:
        raise HTTPException(status_code=400, detail=f"알 수 없는 조명: {light_preset}")
    use_intro = intro.lower().strip() == "true"

    # Build the render command for the chosen engine.
    if engine == "modal":
        modal_exe = shutil.which("modal")
        if modal_exe is None:
            raise HTTPException(
                status_code=500,
                detail="modal CLI를 찾을 수 없습니다. trailer3d 환경에 modal이 설치/로그인되어 있어야 합니다.",
            )
        rel_travel_data = travel_data_path.relative_to(ROOT).as_posix()
        command = [
            modal_exe,
            "run",
            str(MODAL_SCRIPT),
            "--mode",
            "quality-fast" if is_quick else "quality",
            "--travel-data",
            rel_travel_data,
        ]
        if theme != "default":
            command += ["--theme", theme]
        if light_preset:
            command += ["--light-preset", light_preset]
        if use_intro:
            command.append("--intro")
        output_marker = "modal"
    else:
        command = [
            sys.executable,
            str(RENDER_SCRIPT),
            "--travel-data",
            str(travel_data_path),
        ]
        if is_quick:
            command.append("--quick")
        if theme != "default":
            command += ["--theme", theme]
        if light_preset:
            command += ["--light-preset", light_preset]
        if use_intro:
            command.append("--intro")
        output_marker = "local"

    # Render in a worker thread (subprocess blocks for minutes).
    render_started = time.perf_counter()
    result = await asyncio.to_thread(
        subprocess.run,
        command,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=RENDER_TIMEOUT_SECONDS,
    )
    elapsed_seconds = round(time.perf_counter() - render_started, 1)

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if result.returncode != 0:
        tail = (stdout + "\n" + stderr)[-4000:]
        raise HTTPException(status_code=500, detail=f"렌더링 실패:\n{tail}")

    output_name = parse_output_name(stdout, output_marker)
    if output_name is None:
        raise HTTPException(status_code=500, detail="출력 파일 경로를 확인할 수 없습니다.")

    return JSONResponse(
        {
            "video_url": f"/output/{output_name}",
            "engine": engine,
            "theme": theme,
            "light_preset": light_preset or None,
            "intro": use_intro,
            "bgm": bgm_path.name if bgm_path else None,
            "elapsed_seconds": elapsed_seconds,
            "log_tail": stdout[-2000:],
        }
    )


def parse_output_name(stdout: str, marker: str = "local") -> str | None:
    """Pull the rendered filename out of the render subprocess's stdout.

    local  -> render_video.py prints "출력 예정 파일: <path>".
    modal  -> modal_render.py's entrypoint prints "저장 위치: <path>"
              (the container's "출력 예정 파일" is a /app path, not the local file).
    """
    pattern = r"저장 위치:\s*(.+)" if marker == "modal" else r"출력 예정 파일:\s*(.+)"
    match = re.search(pattern, stdout)
    if match:
        return Path(match.group(1).strip()).name
    # Fallback: newest mp4 in output/.
    if OUTPUT_DIR.exists():
        mp4s = sorted(OUTPUT_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        if mp4s:
            return mp4s[-1].name
    return None


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8200, reload=False)
