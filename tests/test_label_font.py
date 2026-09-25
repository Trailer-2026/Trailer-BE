# -*- coding: utf-8 -*-
"""사진 위 장소명 글씨체 자체 점검 — `python tests/test_label_font.py`.

프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.

지키려는 규칙 하나: **표지(썸네일·인트로)와 영상 도중 사진 위 장소명이 같은 글씨체다.**
두 코드가 갈라져 있어서(`utils/cover_image.py` ↔ `services/videoMaker/render_video.py`)
한쪽 상수만 고치면 첫 장면과 본편의 글씨가 조용히 달라진다 — 렌더를 돌려 눈으로 보기 전엔
아무도 모른다. 그 조용한 어긋남을 여기서 잡는다.

Modal 쪽도 같이 본다. 컨테이너에는 한글 폰트가 깔려 있어(fonts-noto-cjk) 레포 폰트가
안 올라가도 **에러 없이 고딕으로 그려진다** — 배포에서만 라벨 글씨가 다른 상태가 된다.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services" / "videoMaker"))

from PIL import Image

import render_video
from utils import cover_image


def test_same_font_constant():
    """표지와 장소명이 같은 폰트 파일을 가리킨다."""
    assert render_video.LABEL_FONT == cover_image.TITLE_FONT, (
        f"표지({cover_image.TITLE_FONT})와 장소명({render_video.LABEL_FONT})의 "
        "글씨체가 갈렸다 — 둘 중 하나만 바꾸지 마라"
    )


def test_bundled_font_is_used():
    """레포 폰트가 있으면 시스템 폰트가 아니라 그것을 쓴다."""
    bundled = cover_image.FONTS_DIR / cover_image.TITLE_FONT
    assert bundled.exists(), f"레포 폰트가 없다: {bundled}"

    chosen = render_video.find_label_font()
    assert chosen is not None
    assert chosen.resolve() == bundled.resolve(), (
        f"레포 폰트를 두고 {chosen} 로 떨어졌다"
    )
    assert cover_image._find_font_path() == str(bundled)


def test_label_is_drawn_and_empty_name_is_not():
    """이름이 있으면 흰 글씨 띠가 나오고, 빈 이름이면 아무것도 안 그린다."""
    assert render_video.place_label_overlay("", 1080, 1920) is None

    overlay = render_video.place_label_overlay("국립민속박물관", 1080, 1920)
    assert overlay is not None and overlay.mode == "RGBA"
    assert overlay.size == (1080, round(1920 * 0.16))
    # 흰 글씨가 실제로 찍혔는지 — 폰트가 한글을 못 그리면 두부(□)나 빈 띠가 된다.
    from PIL import ImageChops

    red, _, _, alpha = overlay.split()
    opaque = alpha.point(lambda v: 255 if v > 200 else 0)
    bright = red.point(lambda v: 255 if v > 240 else 0)
    white = ImageChops.multiply(opaque, bright).histogram()[255]
    assert white > 2000, f"흰 글씨 픽셀이 너무 적다({white}) — 글씨가 안 그려졌다"


def test_size_keeps_apparent_height():
    """손글씨체는 같은 pt 에서 작게 앉는다 — 크기 상수가 그만큼 올라가 있어야 한다.

    예전 굵은 고딕(0.058)의 보이는 높이를 기준으로 ±15% 안이면 통과.
    """
    from PIL import ImageDraw, ImageFont

    draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    name = "국립민속박물관"

    def glyph_height(path: str, size: int) -> int:
        box = draw.textbbox((0, 0), name, font=ImageFont.truetype(path, size))
        return box[3] - box[1]

    label = glyph_height(str(render_video.find_label_font()), round(1080 * render_video.LABEL_SIZE))
    gothic_path = next(
        (p for p in ("C:/Windows/Fonts/malgunbd.ttf",
                     "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")
         if Path(p).exists()),
        None,
    )
    if gothic_path is None:  # 비교 대상 고딕이 없는 환경 — 이 점검만 건너뛴다
        print("  (기준 고딕 폰트가 없어 크기 비교는 건너뜀)")
        return
    before = glyph_height(gothic_path, round(1080 * 0.058))
    assert abs(label - before) / before < 0.15, (
        f"보이는 글자 높이가 예전({before}px)과 {label}px 로 너무 달라졌다"
    )


def test_modal_ships_the_font():
    """Modal 이미지에 레포 폰트가 올라간다 — 안 올라가도 에러가 안 나서 여기서 본다."""
    source = (ROOT / "services" / "videoMaker" / "modal_render.py").read_text(encoding="utf-8")
    assert '"/app/assets/fonts"' in source, (
        "modal_render.py 가 레포 폰트를 컨테이너로 안 올린다 — 배포 영상만 라벨 글씨가 달라진다"
    )
    # 컨테이너 경로(/app/assets/fonts)를 렌더러가 실제로 찾아본다.
    container = render_video.ROOT / "assets" / "fonts"
    assert container in render_video.BUNDLED_FONT_DIRS, (
        f"{container} 를 안 뒤져서 컨테이너에 올려도 못 찾는다"
    )


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            print(f"OK {name}")
    print("모든 점검 통과")
