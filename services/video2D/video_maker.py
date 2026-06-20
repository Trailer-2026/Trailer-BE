from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import imageio.v2 as imageio
import numpy as np
import requests
from filelock import FileLock
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TILE_CACHE_DIR = BASE_DIR / "cache" / "map_tiles"
DEFAULT_OUTPUT_DIR = BASE_DIR / "output"

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
VIDEO_ASPECT = VIDEO_WIDTH / VIDEO_HEIGHT
FPS = 30
TILE_SIZE = 256
OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_USER_AGENT = "Trailer-BE TravelShorts/1.0 (+https://www.openstreetmap.org)"

TRAVEL_ZOOM = 2.55
ARRIVAL_ZOOM = 3.0
LOOK_AHEAD_RATIO = 0.16
CAMERA_SMOOTHING = 0.13
MARKER_VERTICAL_OFFSET = 0.08
OUTPUT_NAME_RE = re.compile(r"^travel_shorts_(\d+)\.mp4$")

logger = logging.getLogger(__name__)


def create_travel_shorts(
    trip_data: dict[str, Any],
    output_path: str | Path | None = None,
) -> Path:
    """
    Create a 9:16 travel shorts video from GPS route data and optional photos.

    If output_path is None, the video is saved as
    services/videoMake/output/travel_shorts_{id}.mp4 using a file-lock-protected
    increasing ID. If output_path is provided, that exact path is used.
    """
    points = _validate_trip_data(trip_data)
    output = _prepare_output_path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_TILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    temp_output = output.with_name(f"{output.stem}.tmp{output.suffix}")
    if temp_output.exists():
        temp_output.unlink()

    title = str(trip_data.get("trip_title") or "Travel Route")
    total_distance_km = _total_distance_km(points)
    layout = _build_map_layout(points, DEFAULT_TILE_CACHE_DIR)
    route_pixels = [layout.latlon_to_image_xy(point["lat"], point["lon"]) for point in points]

    writer = imageio.get_writer(
        temp_output,
        fps=FPS,
        codec="libx264",
        pixelformat="yuv420p",
        quality=8,
        macro_block_size=1,
        ffmpeg_params=[
            "-movflags",
            "+faststart",
            "-preset",
            "medium",
        ],
    )

    try:
        camera_xy = _write_intro_frames(writer, layout, route_pixels, title, points)
        _write_point_photo_if_available(writer, points[0])
        for segment_index in range(len(points) - 1):
            camera_xy = _write_map_segment(
                writer=writer,
                layout=layout,
                route_pixels=route_pixels,
                points=points,
                title=title,
                segment_index=segment_index,
                camera_xy=camera_xy,
            )
            destination = points[segment_index + 1]
            has_photo = _point_has_existing_photo(destination)
            camera_xy = _write_arrival_hold(
                writer=writer,
                layout=layout,
                route_pixels=route_pixels,
                points=points,
                title=title,
                segment_index=segment_index,
                camera_xy=camera_xy,
                has_photo=has_photo,
            )
            _write_point_photo_if_available(writer, destination)

        _write_final_scene(
            writer=writer,
            layout=layout,
            route_pixels=route_pixels,
            title=title,
            point_count=len(points),
            total_distance_km=total_distance_km,
        )
    except Exception:
        writer.close()
        if temp_output.exists():
            temp_output.unlink()
        raise
    else:
        writer.close()

    if not temp_output.exists() or temp_output.stat().st_size == 0:
        raise RuntimeError(f"Video file was not created: {temp_output}")
    temp_output.replace(output)
    logger.info("Created travel shorts video: %s", output)
    return output


def _prepare_output_path(output_path: str | Path | None) -> Path:
    if output_path is not None:
        return _resolve_path(output_path)
    return _allocate_next_output_path(DEFAULT_OUTPUT_DIR)


def _allocate_next_output_path(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / ".video_id.lock"
    counter_path = output_dir / ".video_counter"
    with FileLock(str(lock_path)):
        max_file_id = _max_existing_video_id(output_dir)
        counter_id = _read_counter_id(counter_path)
        next_id = max(max_file_id, counter_id) + 1
        counter_path.write_text(str(next_id), encoding="utf-8")
        output_path = output_dir / f"travel_shorts_{next_id}.mp4"
    logger.info("Allocated travel shorts output ID %s: %s", next_id, output_path)
    return output_path


def _max_existing_video_id(output_dir: Path) -> int:
    max_id = 0
    for path in output_dir.glob("travel_shorts_*.mp4"):
        match = OUTPUT_NAME_RE.match(path.name)
        if match:
            max_id = max(max_id, int(match.group(1)))
    return max_id


def _read_counter_id(counter_path: Path) -> int:
    try:
        return max(0, int(counter_path.read_text(encoding="utf-8").strip()))
    except (FileNotFoundError, ValueError):
        return 0


def _validate_trip_data(trip_data: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(trip_data, dict):
        raise ValueError("trip_data must be a dictionary.")
    raw_points = trip_data.get("points")
    if not isinstance(raw_points, list):
        raise ValueError("trip_data['points'] must be a list.")
    if len(raw_points) < 2:
        raise ValueError("At least two GPS points are required to create a route video.")

    points: list[dict[str, Any]] = []
    for index, raw_point in enumerate(raw_points):
        if not isinstance(raw_point, dict):
            raise ValueError(f"Point #{index + 1} must be a dictionary.")
        try:
            lat = float(raw_point["lat"])
            lon = float(raw_point["lon"])
        except KeyError as exc:
            raise ValueError(f"Point #{index + 1} is missing '{exc.args[0]}'.") from exc
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Point #{index + 1} has invalid lat/lon values.") from exc
        if not -90 <= lat <= 90:
            raise ValueError(f"Point #{index + 1} latitude must be between -90 and 90.")
        if not -180 <= lon <= 180:
            raise ValueError(f"Point #{index + 1} longitude must be between -180 and 180.")

        point = dict(raw_point)
        point["lat"] = lat
        point["lon"] = lon
        point["name"] = str(point.get("name") or point.get("name_en") or f"Point {index + 1}")
        point["name_en"] = str(point.get("name_en") or point["name"])
        point["timestamp"] = str(point.get("timestamp") or "")
        points.append(point)
    return points


class _MapLayout:
    def __init__(self, image: Image.Image, zoom: int, min_pixel_x: int, min_pixel_y: int) -> None:
        self.image = image
        self.zoom = zoom
        self.min_pixel_x = min_pixel_x
        self.min_pixel_y = min_pixel_y
        self.base_crop_width, self.base_crop_height = _max_aspect_crop_size(image.width, image.height)

    def latlon_to_image_xy(self, lat: float, lon: float) -> tuple[float, float]:
        pixel_x, pixel_y = _latlon_to_global_pixel(lat, lon, self.zoom)
        return pixel_x - self.min_pixel_x, pixel_y - self.min_pixel_y


def _build_map_layout(points: list[dict[str, Any]], cache_dir: Path) -> _MapLayout:
    zoom = _choose_zoom(points)
    requested_left, requested_top, requested_right, requested_bottom = _source_bounds_for_zoom(points, zoom)

    min_tile_x = requested_left // TILE_SIZE
    max_tile_x = requested_right // TILE_SIZE
    min_tile_y = requested_top // TILE_SIZE
    max_tile_y = requested_bottom // TILE_SIZE

    source_width = (max_tile_x - min_tile_x + 1) * TILE_SIZE
    source_height = (max_tile_y - min_tile_y + 1) * TILE_SIZE
    mosaic = Image.new("RGB", (source_width, source_height), (236, 232, 224))

    for tile_x in range(min_tile_x, max_tile_x + 1):
        for tile_y in range(min_tile_y, max_tile_y + 1):
            tile = _get_osm_tile(zoom, tile_x, tile_y, cache_dir)
            mosaic.paste(tile, ((tile_x - min_tile_x) * TILE_SIZE, (tile_y - min_tile_y) * TILE_SIZE))

    crop_left = requested_left - min_tile_x * TILE_SIZE
    crop_top = requested_top - min_tile_y * TILE_SIZE
    crop_width = max(1, requested_right - requested_left)
    crop_height = max(1, requested_bottom - requested_top)
    source_map = mosaic.crop((crop_left, crop_top, crop_left + crop_width, crop_top + crop_height))
    source_map = _soften_map(source_map)
    logger.info("Built OSM source map z=%s size=%sx%s", zoom, source_map.width, source_map.height)
    return _MapLayout(source_map, zoom, requested_left, requested_top)


def _choose_zoom(points: list[dict[str, Any]]) -> int:
    for zoom in range(14, 5, -1):
        left, top, right, bottom = _source_bounds_for_zoom(points, zoom)
        tile_count = ((right // TILE_SIZE) - (left // TILE_SIZE) + 1) * ((bottom // TILE_SIZE) - (top // TILE_SIZE) + 1)
        route_width, route_height = _route_size_at_zoom(points, zoom)
        if tile_count <= 140 and max(route_width, route_height) >= 500:
            return zoom
    return 6


def _source_bounds_for_zoom(points: list[dict[str, Any]], zoom: int) -> tuple[int, int, int, int]:
    route_pixels = [_latlon_to_global_pixel(point["lat"], point["lon"], zoom) for point in points]
    min_x = min(x for x, _ in route_pixels)
    max_x = max(x for x, _ in route_pixels)
    min_y = min(y for _, y in route_pixels)
    max_y = max(y for _, y in route_pixels)

    route_width = max(max_x - min_x, 1)
    route_height = max(max_y - min_y, 1)
    pad_x = max(int(route_width * 0.62), 760)
    pad_y = max(int(route_height * 0.55), 1100)

    max_pixel = TILE_SIZE * (2**zoom)
    requested_left = max(0, int(math.floor(min_x - pad_x)))
    requested_right = min(max_pixel - 1, int(math.ceil(max_x + pad_x)))
    requested_top = max(0, int(math.floor(min_y - pad_y)))
    requested_bottom = min(max_pixel - 1, int(math.ceil(max_y + pad_y)))
    return requested_left, requested_top, requested_right, requested_bottom


def _route_size_at_zoom(points: list[dict[str, Any]], zoom: int) -> tuple[float, float]:
    route_pixels = [_latlon_to_global_pixel(point["lat"], point["lon"], zoom) for point in points]
    return (
        max(x for x, _ in route_pixels) - min(x for x, _ in route_pixels),
        max(y for _, y in route_pixels) - min(y for _, y in route_pixels),
    )


def _get_osm_tile(zoom: int, tile_x: int, tile_y: int, cache_dir: Path) -> Image.Image:
    tile_path = cache_dir / str(zoom) / str(tile_x) / f"{tile_y}.png"
    if tile_path.exists():
        return Image.open(tile_path).convert("RGB")

    tile_path.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": OSM_USER_AGENT}
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.get(
                OSM_TILE_URL.format(z=zoom, x=tile_x, y=tile_y),
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()
            tile = Image.open(BytesIO(response.content)).convert("RGB")
            tile.save(tile_path)
            time.sleep(0.08)
            return tile
        except (requests.RequestException, OSError) as exc:
            last_error = exc
            logger.warning(
                "OSM tile download failed (attempt %s/3, z=%s x=%s y=%s): %s",
                attempt + 1,
                zoom,
                tile_x,
                tile_y,
                exc,
            )
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Failed to download OSM tile z={zoom} x={tile_x} y={tile_y}") from last_error


def _latlon_to_global_pixel(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    sin_lat = math.sin(math.radians(lat))
    map_size = TILE_SIZE * (2**zoom)
    x = (lon + 180.0) / 360.0 * map_size
    y = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * map_size
    return x, y


def _write_intro_frames(
    writer: Any,
    layout: _MapLayout,
    route_pixels: list[tuple[float, float]],
    title: str,
    points: list[dict[str, Any]],
) -> tuple[float, float]:
    overview_frames = int(FPS * 0.8)
    for frame_index in range(overview_frames):
        frame = _compose_overview_frame(
            layout=layout,
            route_pixels=route_pixels,
            title=title,
            subtitle=f"{points[0]['name_en']} to {points[-1]['name_en']}",
            marker_world=route_pixels[0],
            progress=0.0,
        )
        writer.append_data(_to_frame_array(frame))

    start = route_pixels[0]
    next_point = route_pixels[1]
    zoom_frames = int(FPS * 0.85)
    overview_camera = (layout.image.width / 2, layout.image.height / 2)
    target_camera = _camera_target(layout, start, next_point, TRAVEL_ZOOM, look_ahead=True)
    camera_xy = overview_camera

    for frame_index in range(zoom_frames):
        progress = _ease_in_out(frame_index / max(zoom_frames - 1, 1))
        zoom = _lerp(1.0, TRAVEL_ZOOM, progress)
        desired = (_lerp(overview_camera[0], target_camera[0], progress), _lerp(overview_camera[1], target_camera[1], progress))
        camera_xy = _smooth_camera(layout, camera_xy, desired, zoom, smoothing=0.22)
        frame = _compose_camera_map_frame(
            layout=layout,
            route_pixels=route_pixels,
            points=points,
            title=title,
            subtitle=f"Starting from {points[0]['name_en']}",
            progress=0.0,
            marker_world=start,
            camera_xy=camera_xy,
            zoom=zoom,
        )
        writer.append_data(_to_frame_array(frame))
    return camera_xy


def _write_map_segment(
    writer: Any,
    layout: _MapLayout,
    route_pixels: list[tuple[float, float]],
    points: list[dict[str, Any]],
    title: str,
    segment_index: int,
    camera_xy: tuple[float, float],
) -> tuple[float, float]:
    move_frames = int(FPS * 1.85)
    start = route_pixels[segment_index]
    end = route_pixels[segment_index + 1]
    subtitle = f"{points[segment_index]['name_en']} -> {points[segment_index + 1]['name_en']}"

    for frame_index in range(move_frames):
        raw_progress = frame_index / max(move_frames - 1, 1)
        eased_progress = _ease_in_out(raw_progress)
        current = (_lerp(start[0], end[0], eased_progress), _lerp(start[1], end[1], eased_progress))
        arrival_ratio = _ease_in_out(max(0.0, (raw_progress - 0.72) / 0.28))
        zoom = _lerp(TRAVEL_ZOOM, ARRIVAL_ZOOM, arrival_ratio)
        target = _camera_target(layout, current, end, zoom, look_ahead=True)
        camera_xy = _smooth_camera(layout, camera_xy, target, zoom)
        route_progress = (segment_index + eased_progress) / (len(route_pixels) - 1)
        frame = _compose_camera_map_frame(
            layout=layout,
            route_pixels=route_pixels,
            points=points,
            title=title,
            subtitle=subtitle,
            progress=route_progress,
            marker_world=current,
            camera_xy=camera_xy,
            zoom=zoom,
        )
        if frame_index < 8:
            frame = Image.blend(Image.new("RGB", frame.size, (12, 14, 16)), frame, frame_index / 8)
        writer.append_data(_to_frame_array(frame))
    return camera_xy


def _write_arrival_hold(
    writer: Any,
    layout: _MapLayout,
    route_pixels: list[tuple[float, float]],
    points: list[dict[str, Any]],
    title: str,
    segment_index: int,
    camera_xy: tuple[float, float],
    has_photo: bool,
) -> tuple[float, float]:
    hold_frames = int(FPS * (0.75 if has_photo else 0.45))
    destination = route_pixels[segment_index + 1]
    route_progress = (segment_index + 1) / (len(route_pixels) - 1)
    subtitle = f"Arrived at {points[segment_index + 1]['name_en']}"

    for frame_index in range(hold_frames):
        target = _camera_target(layout, destination, destination, ARRIVAL_ZOOM, look_ahead=False)
        camera_xy = _smooth_camera(layout, camera_xy, target, ARRIVAL_ZOOM, smoothing=0.17)
        pulse = 0.5 + 0.5 * math.sin((frame_index / max(hold_frames - 1, 1)) * math.pi * 3)
        frame = _compose_camera_map_frame(
            layout=layout,
            route_pixels=route_pixels,
            points=points,
            title=title,
            subtitle=subtitle,
            progress=route_progress,
            marker_world=destination,
            camera_xy=camera_xy,
            zoom=ARRIVAL_ZOOM,
            pulse_strength=pulse,
        )
        writer.append_data(_to_frame_array(frame))
    return camera_xy


def _point_has_existing_photo(point: dict[str, Any]) -> bool:
    photo_path = point.get("photo")
    return bool(photo_path and _resolve_path(photo_path).exists())


def _write_point_photo_if_available(writer: Any, point: dict[str, Any]) -> bool:
    photo_path = point.get("photo")
    if not photo_path:
        return False
    resolved_photo = _resolve_path(photo_path)
    # Invalid photo paths do not fail the whole video; only that photo scene is skipped.
    if resolved_photo.exists():
        _write_photo_scene(writer, resolved_photo, point)
        return True
    logger.warning("Photo not found, skipping photo scene: %s", resolved_photo)
    return False


def _compose_camera_map_frame(
    layout: _MapLayout,
    route_pixels: list[tuple[float, float]],
    points: list[dict[str, Any]],
    title: str,
    subtitle: str,
    progress: float,
    marker_world: tuple[float, float],
    camera_xy: tuple[float, float],
    zoom: float,
    pulse_strength: float = 0.0,
) -> Image.Image:
    crop = _camera_crop(layout, camera_xy, zoom)
    map_crop = layout.image.crop((crop[0], crop[1], crop[0] + crop[2], crop[1] + crop[3]))
    frame = map_crop.resize((VIDEO_WIDTH, VIDEO_HEIGHT), Image.Resampling.LANCZOS).convert("RGBA")
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    fonts = _load_fonts()

    visited, remaining = _split_route_by_progress(route_pixels, progress)
    if len(remaining) >= 2:
        _draw_polyline(draw, [_world_to_screen(point, crop) for point in remaining], fill=(250, 250, 250, 135), width=9)
    if len(visited) >= 2:
        screen_visited = [_world_to_screen(point, crop) for point in visited]
        _draw_polyline(draw, screen_visited, fill=(10, 22, 28, 225), width=17)
        _draw_polyline(draw, screen_visited, fill=(4, 184, 147, 255), width=9)

    for index, point_world in enumerate(route_pixels):
        screen_xy = _world_to_screen(point_world, crop)
        if not _is_visible(screen_xy, margin=80):
            continue
        has_photo = bool(points[index].get("photo"))
        radius = 15 if index in (0, len(route_pixels) - 1) else 11
        fill = (255, 214, 102, 255) if has_photo else (255, 255, 255, 245)
        draw.ellipse(
            (screen_xy[0] - radius, screen_xy[1] - radius, screen_xy[0] + radius, screen_xy[1] + radius),
            fill=fill,
            outline=(28, 42, 48, 235),
            width=4,
        )

    marker_screen = _world_to_screen(marker_world, crop)
    marker_radius = int(24 + 10 * pulse_strength)
    draw.ellipse(
        (
            marker_screen[0] - 48 - 10 * pulse_strength,
            marker_screen[1] - 48 - 10 * pulse_strength,
            marker_screen[0] + 48 + 10 * pulse_strength,
            marker_screen[1] + 48 + 10 * pulse_strength,
        ),
        fill=(4, 184, 147, 58),
    )
    draw.ellipse(
        (
            marker_screen[0] - marker_radius,
            marker_screen[1] - marker_radius,
            marker_screen[0] + marker_radius,
            marker_screen[1] + marker_radius,
        ),
        fill=(3, 132, 108, 255),
        outline=(255, 255, 255, 255),
        width=8,
    )
    draw.ellipse(
        (marker_screen[0] - 7, marker_screen[1] - 7, marker_screen[0] + 7, marker_screen[1] + 7),
        fill=(255, 255, 255, 255),
    )

    _draw_map_ui(draw, fonts, title, subtitle, progress)
    return Image.alpha_composite(frame, overlay).convert("RGB")


def _draw_map_ui(
    draw: ImageDraw.ImageDraw,
    fonts: dict[str, ImageFont.ImageFont],
    title: str,
    subtitle: str,
    progress: float,
) -> None:
    draw.rounded_rectangle((54, 54, VIDEO_WIDTH - 54, 222), radius=34, fill=(18, 22, 26, 214))
    draw.text((90, 82), title, fill=(255, 255, 255, 255), font=fonts["title"])
    draw.text((92, 154), subtitle, fill=(220, 235, 238, 235), font=fonts["body"])

    draw.rounded_rectangle((68, VIDEO_HEIGHT - 168, VIDEO_WIDTH - 68, VIDEO_HEIGHT - 92), radius=24, fill=(18, 22, 26, 185))
    bar_left = 108
    bar_top = VIDEO_HEIGHT - 138
    bar_right = VIDEO_WIDTH - 108
    draw.rounded_rectangle((bar_left, bar_top, bar_right, bar_top + 16), radius=8, fill=(255, 255, 255, 82))
    draw.rounded_rectangle(
        (bar_left, bar_top, bar_left + (bar_right - bar_left) * _clamp(progress, 0.0, 1.0), bar_top + 16),
        radius=8,
        fill=(4, 184, 147, 255),
    )


def _compose_overview_frame(
    layout: _MapLayout,
    route_pixels: list[tuple[float, float]],
    title: str,
    subtitle: str,
    marker_world: tuple[float, float] | None,
    progress: float,
    final_stats: str | None = None,
) -> Image.Image:
    background = _center_crop_cover(layout.image, VIDEO_WIDTH, VIDEO_HEIGHT)
    background = background.filter(ImageFilter.GaussianBlur(radius=22))
    background = ImageEnhance.Brightness(background).enhance(0.68).convert("RGBA")
    overlay = Image.new("RGBA", background.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    fonts = _load_fonts()

    map_top = 270 if final_stats is None else 455
    map_bottom_margin = 240 if final_stats is None else 330
    scale = min((VIDEO_WIDTH - 112) / layout.image.width, (VIDEO_HEIGHT - map_top - map_bottom_margin) / layout.image.height)
    map_width = int(layout.image.width * scale)
    map_height = int(layout.image.height * scale)
    map_left = (VIDEO_WIDTH - map_width) // 2
    map_image = layout.image.resize((map_width, map_height), Image.Resampling.LANCZOS).convert("RGBA")
    shadow = Image.new("RGBA", background.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((map_left - 12, map_top + 18, map_left + map_width + 12, map_top + map_height + 30), radius=34, fill=(0, 0, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    background = Image.alpha_composite(background, shadow)
    background.paste(map_image, (map_left, map_top))
    draw.rounded_rectangle((map_left, map_top, map_left + map_width, map_top + map_height), radius=24, outline=(255, 255, 255, 210), width=5)

    def transform(point: tuple[float, float]) -> tuple[float, float]:
        return map_left + point[0] * scale, map_top + point[1] * scale

    visited, remaining = _split_route_by_progress(route_pixels, progress)
    _draw_polyline(draw, [transform(point) for point in remaining], fill=(255, 255, 255, 165), width=8)
    _draw_polyline(draw, [transform(point) for point in visited], fill=(7, 28, 36, 230), width=15)
    _draw_polyline(draw, [transform(point) for point in visited], fill=(4, 184, 147, 255), width=8)
    for point in route_pixels:
        x, y = transform(point)
        draw.ellipse((x - 10, y - 10, x + 10, y + 10), fill=(255, 255, 255, 255), outline=(3, 132, 108, 255), width=4)
    if marker_world:
        x, y = transform(marker_world)
        draw.ellipse((x - 24, y - 24, x + 24, y + 24), fill=(3, 132, 108, 255), outline=(255, 255, 255, 255), width=7)

    draw.rounded_rectangle((54, 54, VIDEO_WIDTH - 54, 222), radius=34, fill=(18, 22, 26, 214))
    draw.text((90, 82), title, fill=(255, 255, 255, 255), font=fonts["title"])
    draw.text((92, 154), subtitle, fill=(220, 235, 238, 235), font=fonts["body"])

    if final_stats:
        draw.rounded_rectangle((70, VIDEO_HEIGHT - 302, VIDEO_WIDTH - 70, VIDEO_HEIGHT - 118), radius=40, fill=(13, 18, 22, 222))
        draw.text((112, VIDEO_HEIGHT - 250), "\uc624\ub298\uc758 \uc5ec\ud589 \uae30\ub85d", fill=(255, 255, 255, 255), font=fonts["subtitle"])
        draw.text((112, VIDEO_HEIGHT - 174), final_stats, fill=(222, 238, 238, 245), font=fonts["body"])

    return Image.alpha_composite(background, overlay).convert("RGB")


def _split_route_by_progress(
    route_pixels: list[tuple[float, float]],
    progress: float,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    progress = _clamp(progress, 0.0, 1.0)
    if progress >= 1:
        return route_pixels, [route_pixels[-1]]
    segment_float = progress * (len(route_pixels) - 1)
    segment_index = min(int(math.floor(segment_float)), len(route_pixels) - 2)
    local_t = segment_float - segment_index
    start = route_pixels[segment_index]
    end = route_pixels[segment_index + 1]
    current = (_lerp(start[0], end[0], local_t), _lerp(start[1], end[1], local_t))
    visited = route_pixels[: segment_index + 1] + [current]
    remaining = [current] + route_pixels[segment_index + 1 :]
    return visited, remaining


def _write_photo_scene(writer: Any, photo_path: Path, point: dict[str, Any]) -> None:
    scene_frames = int(FPS * 1.75)
    original = Image.open(photo_path).convert("RGB")
    fonts = _load_fonts()
    label = str(point.get("name_en") or point.get("name") or "Travel Stop")
    timestamp = _format_timestamp(str(point.get("timestamp") or ""))

    for frame_index in range(scene_frames):
        t = frame_index / max(scene_frames - 1, 1)
        zoom = 1.0 + 0.055 * t
        frame = _compose_photo_frame(original, label, timestamp, fonts, zoom)
        if frame_index < 8:
            frame = Image.blend(Image.new("RGB", frame.size, (12, 14, 16)), frame, frame_index / 8)
        elif frame_index > scene_frames - 9:
            alpha = (scene_frames - frame_index - 1) / 8
            frame = Image.blend(Image.new("RGB", frame.size, (12, 14, 16)), frame, max(alpha, 0))
        writer.append_data(_to_frame_array(frame))


def _compose_photo_frame(
    photo: Image.Image,
    label: str,
    timestamp: str,
    fonts: dict[str, ImageFont.ImageFont],
    zoom: float,
) -> Image.Image:
    background = _center_crop_cover(photo, VIDEO_WIDTH, VIDEO_HEIGHT)
    background = background.filter(ImageFilter.GaussianBlur(radius=28))
    background = ImageEnhance.Brightness(background).enhance(0.62)

    foreground_width = int(VIDEO_WIDTH * 0.86 * zoom)
    foreground_height = int(VIDEO_HEIGHT * 0.66 * zoom)
    foreground = _center_crop_cover(photo, foreground_width, foreground_height)
    foreground = foreground.resize((foreground_width, foreground_height), Image.Resampling.LANCZOS)

    frame = background.convert("RGBA")
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    card_left = (VIDEO_WIDTH - foreground_width) // 2
    card_top = int(VIDEO_HEIGHT * 0.18) - int((zoom - 1.0) * 160)
    shadow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        (card_left - 12, card_top + 18, card_left + foreground_width + 12, card_top + foreground_height + 30),
        radius=42,
        fill=(0, 0, 0, 115),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(16))
    frame = Image.alpha_composite(frame, shadow)

    mask = Image.new("L", (foreground_width, foreground_height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, foreground_width, foreground_height), radius=36, fill=255)
    frame.paste(foreground.convert("RGBA"), (card_left, card_top), mask)
    draw.rounded_rectangle(
        (card_left, card_top, card_left + foreground_width, card_top + foreground_height),
        radius=36,
        outline=(255, 255, 255, 210),
        width=5,
    )

    label_box_top = VIDEO_HEIGHT - 330
    draw.rounded_rectangle((68, label_box_top, VIDEO_WIDTH - 68, label_box_top + 202), radius=34, fill=(12, 16, 19, 220))
    draw.text((104, label_box_top + 36), label, fill=(255, 255, 255, 255), font=fonts["title"])
    draw.text((106, label_box_top + 114), timestamp, fill=(218, 232, 232, 235), font=fonts["body"])
    draw.text((106, label_box_top + 156), "Travel memory", fill=(4, 184, 147, 255), font=fonts["small"])

    return Image.alpha_composite(frame, overlay).convert("RGB")


def _write_final_scene(
    writer: Any,
    layout: _MapLayout,
    route_pixels: list[tuple[float, float]],
    title: str,
    point_count: int,
    total_distance_km: float,
) -> None:
    frames = int(FPS * 2.2)
    subtitle = f"{point_count} stops  |  {total_distance_km:.1f} km"
    final_stats = f"Total route: {total_distance_km:.1f} km"
    for frame_index in range(frames):
        frame = _compose_overview_frame(
            layout=layout,
            route_pixels=route_pixels,
            title=title,
            subtitle=subtitle,
            marker_world=None,
            progress=1.0,
            final_stats=final_stats,
        )
        if frame_index < 10:
            frame = Image.blend(Image.new("RGB", frame.size, (12, 14, 16)), frame, frame_index / 10)
        writer.append_data(_to_frame_array(frame))


def _camera_target(
    layout: _MapLayout,
    current: tuple[float, float],
    end: tuple[float, float],
    zoom: float,
    look_ahead: bool,
) -> tuple[float, float]:
    _, crop_height = _crop_size_for_zoom(layout, zoom)
    direction_x = end[0] - current[0]
    direction_y = end[1] - current[1]
    look_ratio = LOOK_AHEAD_RATIO if look_ahead else 0.0
    target_x = current[0] + direction_x * look_ratio
    target_y = current[1] + direction_y * look_ratio - crop_height * MARKER_VERTICAL_OFFSET
    return _clamp_camera(layout, (target_x, target_y), zoom)


def _smooth_camera(
    layout: _MapLayout,
    previous: tuple[float, float],
    target: tuple[float, float],
    zoom: float,
    smoothing: float = CAMERA_SMOOTHING,
) -> tuple[float, float]:
    camera = (_lerp(previous[0], target[0], smoothing), _lerp(previous[1], target[1], smoothing))
    return _clamp_camera(layout, camera, zoom)


def _camera_crop(layout: _MapLayout, camera_xy: tuple[float, float], zoom: float) -> tuple[int, int, int, int]:
    crop_width, crop_height = _crop_size_for_zoom(layout, zoom)
    camera_x, camera_y = _clamp_camera(layout, camera_xy, zoom)
    left = int(round(camera_x - crop_width / 2))
    top = int(round(camera_y - crop_height / 2))
    left = int(_clamp(left, 0, max(0, layout.image.width - crop_width)))
    top = int(_clamp(top, 0, max(0, layout.image.height - crop_height)))
    return left, top, crop_width, crop_height


def _crop_size_for_zoom(layout: _MapLayout, zoom: float) -> tuple[int, int]:
    zoom = max(1.0, zoom)
    crop_height = max(160, int(layout.base_crop_height / zoom))
    crop_width = max(90, int(crop_height * VIDEO_ASPECT))
    if crop_width > layout.image.width:
        crop_width = layout.image.width
        crop_height = int(crop_width / VIDEO_ASPECT)
    if crop_height > layout.image.height:
        crop_height = layout.image.height
        crop_width = int(crop_height * VIDEO_ASPECT)
    crop_width = min(crop_width, layout.image.width)
    crop_height = min(crop_height, layout.image.height)
    return max(1, crop_width), max(1, crop_height)


def _max_aspect_crop_size(width: int, height: int) -> tuple[int, int]:
    if width / height >= VIDEO_ASPECT:
        crop_height = height
        crop_width = int(crop_height * VIDEO_ASPECT)
    else:
        crop_width = width
        crop_height = int(crop_width / VIDEO_ASPECT)
    return max(1, crop_width), max(1, crop_height)


def _clamp_camera(layout: _MapLayout, camera_xy: tuple[float, float], zoom: float) -> tuple[float, float]:
    crop_width, crop_height = _crop_size_for_zoom(layout, zoom)
    min_x = crop_width / 2
    max_x = layout.image.width - crop_width / 2
    min_y = crop_height / 2
    max_y = layout.image.height - crop_height / 2
    if min_x > max_x:
        clamped_x = layout.image.width / 2
    else:
        clamped_x = _clamp(camera_xy[0], min_x, max_x)
    if min_y > max_y:
        clamped_y = layout.image.height / 2
    else:
        clamped_y = _clamp(camera_xy[1], min_y, max_y)
    return clamped_x, clamped_y


def _world_to_screen(point: tuple[float, float], crop: tuple[int, int, int, int]) -> tuple[float, float]:
    crop_left, crop_top, crop_width, crop_height = crop
    return (
        (point[0] - crop_left) * VIDEO_WIDTH / crop_width,
        (point[1] - crop_top) * VIDEO_HEIGHT / crop_height,
    )


def _is_visible(point: tuple[float, float], margin: int = 0) -> bool:
    return -margin <= point[0] <= VIDEO_WIDTH + margin and -margin <= point[1] <= VIDEO_HEIGHT + margin


def _draw_polyline(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[float, float]],
    fill: tuple[int, int, int, int],
    width: int,
) -> None:
    if len(points) < 2:
        return
    draw.line([(int(x), int(y)) for x, y in points], fill=fill, width=width, joint="curve")


def _soften_map(image: Image.Image) -> Image.Image:
    image = ImageEnhance.Color(image).enhance(0.72)
    image = ImageEnhance.Contrast(image).enhance(0.92)
    image = ImageEnhance.Brightness(image).enhance(1.03)
    return image


def _center_crop_cover(image: Image.Image, target_width: int, target_height: int) -> Image.Image:
    scale = max(target_width / image.width, target_height / image.height)
    resized = image.resize(
        (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale)))),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - target_width) // 2)
    top = max(0, (resized.height - target_height) // 2)
    return resized.crop((left, top, left + target_width, top + target_height))


def _to_frame_array(image: Image.Image) -> np.ndarray:
    if image.size != (VIDEO_WIDTH, VIDEO_HEIGHT):
        image = image.resize((VIDEO_WIDTH, VIDEO_HEIGHT), Image.Resampling.LANCZOS)
    return np.asarray(image.convert("RGB"), dtype=np.uint8)


def _ease_in_out(t: float) -> float:
    t = _clamp(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _lerp(start: float, end: float, ratio: float) -> float:
    return start + (end - start) * ratio


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _haversine_km(point_a: dict[str, Any], point_b: dict[str, Any]) -> float:
    earth_radius_km = 6371.0088
    lat1 = math.radians(float(point_a["lat"]))
    lat2 = math.radians(float(point_b["lat"]))
    delta_lat = math.radians(float(point_b["lat"]) - float(point_a["lat"]))
    delta_lon = math.radians(float(point_b["lon"]) - float(point_a["lon"]))
    a = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return earth_radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _total_distance_km(points: list[dict[str, Any]]) -> float:
    return sum(_haversine_km(points[index], points[index + 1]) for index in range(len(points) - 1))


def _format_timestamp(timestamp: str) -> str:
    if not timestamp:
        return ""
    try:
        parsed = datetime.fromisoformat(timestamp)
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return timestamp


def _resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    repo_root = BASE_DIR.parents[1]
    from_repo_root = repo_root / candidate
    if from_repo_root.exists() or str(candidate).replace("\\", "/").startswith("services/"):
        return from_repo_root
    return BASE_DIR / candidate


def _load_fonts() -> dict[str, ImageFont.ImageFont]:
    candidates = [
        Path("C:/Windows/Fonts/malgun.ttf"),
        Path("C:/Windows/Fonts/malgunbd.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    font_path = next((path for path in candidates if path.exists()), None)

    def load(size: int) -> ImageFont.ImageFont:
        if font_path:
            try:
                return ImageFont.truetype(str(font_path), size=size)
            except OSError:
                logger.warning("Failed to load font: %s", font_path)
        return ImageFont.load_default()

    return {
        "title": load(58),
        "subtitle": load(50),
        "body": load(34),
        "small": load(28),
    }
