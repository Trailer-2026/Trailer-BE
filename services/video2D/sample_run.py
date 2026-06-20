from __future__ import annotations

import json
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from video_maker import BASE_DIR, create_travel_shorts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")

    ensure_directories()
    photos = create_sample_photos()
    trip_data = create_sample_route_json(photos)
    video_path = create_travel_shorts(trip_data=trip_data, output_path=None)
    size_mb = video_path.stat().st_size / (1024 * 1024)
    print(f"created_video={video_path}")
    print(f"file_size_mb={size_mb:.2f}")


def ensure_directories() -> None:
    for directory in [
        BASE_DIR / "data",
        BASE_DIR / "assets" / "photos",
        BASE_DIR / "cache" / "map_tiles",
        BASE_DIR / "output",
    ]:
        directory.mkdir(parents=True, exist_ok=True)


def create_sample_photos() -> dict[str, Path]:
    photo_dir = BASE_DIR / "assets" / "photos"
    specs = {
        "seoul_station": {
            "name": "Seoul Station",
            "date": "2026-06-13 09:00",
            "colors": ((30, 78, 137), (239, 183, 64)),
            "accent": (255, 255, 255),
        },
        "sungsimdang": {
            "name": "Sungsimdang",
            "date": "2026-06-13 11:00",
            "colors": ((118, 38, 44), (245, 179, 83)),
            "accent": (255, 246, 220),
        },
        "yeosu_expo": {
            "name": "Yeosu Expo",
            "date": "2026-06-13 15:30",
            "colors": ((16, 100, 128), (82, 190, 204)),
            "accent": (232, 255, 255),
        },
        "haeundae": {
            "name": "Busan Haeundae",
            "date": "2026-06-13 19:00",
            "colors": ((31, 76, 143), (247, 154, 93)),
            "accent": (255, 244, 220),
        },
    }
    paths: dict[str, Path] = {}
    for slug, spec in specs.items():
        path = photo_dir / f"{slug}.jpg"
        image = _make_travel_card(
            title=spec["name"],
            date=spec["date"],
            start_color=spec["colors"][0],
            end_color=spec["colors"][1],
            accent=spec["accent"],
        )
        image.save(path, quality=92)
        paths[slug] = path
    return paths


def create_sample_route_json(photos: dict[str, Path]) -> dict[str, object]:
    trip_data: dict[str, object] = {
        "trip_title": "Seoul to Busan Trip",
        "points": [
            {
                "name": "\uc11c\uc6b8\uc5ed",
                "name_en": "Seoul Station",
                "lat": 37.5547,
                "lon": 126.9706,
                "timestamp": "2026-06-13T09:00:00",
                "photo": str(photos["seoul_station"]),
            },
            {
                "name": "\ub300\uc804 \uc131\uc2ec\ub2f9",
                "name_en": "Daejeon Sungsimdang",
                "lat": 36.3276,
                "lon": 127.4273,
                "timestamp": "2026-06-13T11:00:00",
                "photo": str(photos["sungsimdang"]),
            },
            {
                "name": "\uc5ec\uc218 \uc5d1\uc2a4\ud3ec",
                "name_en": "Yeosu Expo",
                "lat": 34.7520,
                "lon": 127.7489,
                "timestamp": "2026-06-13T15:30:00",
                "photo": str(photos["yeosu_expo"]),
            },
            {
                "name": "\ubd80\uc0b0 \ud574\uc6b4\ub300",
                "name_en": "Busan Haeundae",
                "lat": 35.1587,
                "lon": 129.1604,
                "timestamp": "2026-06-13T19:00:00",
                "photo": str(photos["haeundae"]),
            },
        ],
    }
    data_path = BASE_DIR / "data" / "sample_route.json"
    data_path.write_text(json.dumps(trip_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return trip_data


def _make_travel_card(
    title: str,
    date: str,
    start_color: tuple[int, int, int],
    end_color: tuple[int, int, int],
    accent: tuple[int, int, int],
) -> Image.Image:
    width, height = 1280, 1700
    image = Image.new("RGB", (width, height), start_color)
    pixels = image.load()
    for y in range(height):
        ratio = y / (height - 1)
        color = tuple(int(start_color[i] * (1 - ratio) + end_color[i] * ratio) for i in range(3))
        for x in range(width):
            pixels[x, y] = color

    draw = ImageDraw.Draw(image, "RGBA")
    font_title = _load_font(96)
    font_body = _load_font(42)
    font_small = _load_font(34)

    draw.ellipse((-190, 90, 430, 710), fill=accent + (54,))
    draw.ellipse((770, 900, 1480, 1610), fill=(255, 255, 255, 44))
    draw.rounded_rectangle((96, 116, width - 96, height - 116), radius=54, outline=(255, 255, 255, 190), width=7)
    draw.rounded_rectangle((150, 1020, width - 150, 1435), radius=46, fill=(15, 18, 24, 172))
    draw.line((210, 220, width - 210, 220), fill=(255, 255, 255, 160), width=4)
    draw.line((210, height - 220, width - 210, height - 220), fill=(255, 255, 255, 160), width=4)

    draw.text((190, 1060), title, font=font_title, fill=(255, 255, 255, 255))
    draw.text((194, 1190), date, font=font_body, fill=(234, 244, 244, 245))
    draw.text((194, 1270), "Travel card", font=font_small, fill=accent + (255,))
    for offset in range(0, 420, 70):
        draw.rounded_rectangle((210 + offset, 360 + offset // 3, 330 + offset, 480 + offset // 3), radius=28, fill=(255, 255, 255, 38))

    return image


def _load_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/malgun.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                pass
    return ImageFont.load_default()


if __name__ == "__main__":
    main()
