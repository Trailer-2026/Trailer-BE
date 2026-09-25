"""릴스 표지(썸네일) 이미지 생성 — 대표 사진 1장에 청량한 보정 + 여행 제목.

`_publish_thumbnail` 의 ffmpeg 프레임 grab 을 대체하는 경로다. 사용자가 고른 대표
사진에만 적용하며, 실패하면 None 을 돌려 호출 측이 기존 grab 으로 폴백한다.

Pillow 만 쓴다 — 렌더 파이프라인(Playwright/Modal)과 무관한 후처리라 서버에서 바로 돈다.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps, ImageStat

logger = logging.getLogger(__name__)

# 썸네일 크기. 홈 피드 카드용이라 영상 해상도(1080x1920)까지 갈 이유가 없다 —
# ffmpeg grab 경로도 scale=540:-2 로 같은 급을 만든다.
WIDTH, HEIGHT = 540, 960

# --- 청량한 보정 계수 (결과 보고 조정하는 값들) ------------------------------- #
SATURATION = 1.12
BRIGHTNESS = 1.08
CONTRAST = 1.08
COOL_SHIFT = (-4, 0, 6)  # R,G,B 오프셋 — 살짝 파랗게 틀어 시원한 느낌을 낸다
TOP_GLOW = 0.12  # 상단 22% 에 얹는 흰색 그라데이션 세기 (제목이 없을 때만)

# --- 제목 --------------------------------------------------------------------- #
TITLE_BAND = 0.26  # 밝기를 재고 글씨를 앉히는 상단 영역 비율
TITLE_SIZE = 0.115  # 글자 크기 = 폭 × 이 값
TITLE_TRACKING = 0.08  # 자간 = 글자 크기 × 이 값 (넓은 자간이 '디자인된' 느낌을 만든다)

# 흰 글씨가 묻히지 않게 상단에 까는 어두운 그라데이션의 세기를 사진 밝기로 정한다.
# 어두운 사진엔 아무것도 안 깔고(0), 밝은 하늘 사진일수록 진하게.
SCRIM_DARK_LUMA = 110.0  # 이보다 어두우면 그라데이션 없음
SCRIM_BRIGHT_LUMA = 200.0  # 이보다 밝으면 최대 세기
SCRIM_MAX = 0.45

# 표지 글씨체. **레포에 넣은 폰트를 먼저 본다** — 개발 기기·배포 서버·Modal 컨테이너가
# 저마다 다른 폰트를 갖고 있어 시스템 폰트에 기대면 같은 제목이 환경마다 다르게 나오고,
# 서버에 한글 폰트가 아예 없으면 제목이 통째로 사라진다(그 땐 경고 로그만 남는다).
# 글씨체를 바꾸려면 assets/fonts/ 에 파일을 넣고 이 한 줄만 고치면 된다.
FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
TITLE_FONT = "Handwriting.ttf"

# 레포 폰트를 못 찾았을 때만 보는 시스템 폰트들 (명조 계열 우선 — 굵은 고딕은 딱딱하다).
# 한글이 없는 폰트로 떨어지면 제목이 두부(□□□)가 되므로 CJK 폰트만 후보에 둔다.
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/NotoSerifKR-VF.ttf",
    "C:/Windows/Fonts/batang.ttc",
    "C:/Windows/Fonts/malgunsl.ttf",  # 맑은 고딕 Semilight — 얇아서 그나마 부드럽다
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSerifKR-Regular.otf",
    "/usr/share/fonts/truetype/nanum/NanumMyeongjo.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)
# 배포 서버(GCP VM)에 폰트가 어디 깔릴지 보장이 없어 경로 목록이 빗나갈 수 있다.
# 그 때를 위한 마지막 수단 — fonts 트리에서 CJK 폰트를 직접 찾는다.
_FONT_GLOBS = ("NotoSerifCJK*", "NotoSerifKR*", "NanumMyeongjo*", "NotoSansCJK*", "NanumGothic*")


def _find_font_path() -> str | None:
    bundled = FONTS_DIR / TITLE_FONT
    if bundled.exists():
        return str(bundled)
    logger.warning("레포 폰트(%s)가 없어 시스템 폰트로 떨어집니다.", bundled)
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    for root in (Path("/usr/share/fonts"), Path("/usr/local/share/fonts")):
        if not root.exists():
            continue
        for pattern in _FONT_GLOBS:
            found = next(root.rglob(pattern), None)
            if found is not None:
                return str(found)
    return None


def _cool_filter(image: Image.Image) -> Image.Image:
    """청량한 보정 — 채도·밝기·대비를 살짝 올리고 색을 시원한 쪽으로 민다."""
    image = ImageEnhance.Color(image).enhance(SATURATION)
    image = ImageEnhance.Brightness(image).enhance(BRIGHTNESS)
    image = ImageEnhance.Contrast(image).enhance(CONTRAST)
    # 채널별 오프셋. point 는 채널마다 룩업테이블을 따로 받는다.
    tables: list[int] = []
    for shift in COOL_SHIFT:
        tables += [min(255, max(0, value + shift)) for value in range(256)]
    return image.point(tables)


def _gradient(width: int, height: int, peak: float, top_dark: bool) -> Image.Image:
    """위(또는 아래)로 갈수록 진해지는 알파 마스크(L)."""
    ramp = Image.linear_gradient("L")  # 위=0, 아래=255
    if top_dark:
        ramp = ramp.point(lambda value: round((255 - value) * peak))
    else:
        ramp = ramp.point(lambda value: round(value * peak))
    return ramp.resize((width, height))


def _band_luma(image: Image.Image, band: int) -> float:
    """상단 띠의 평균 밝기(0~255). 흰 글씨가 묻힐지 판단하는 근거."""
    return ImageStat.Stat(image.crop((0, 0, image.width, band)).convert("L")).mean[0]


def _scrim_strength(luma: float) -> float:
    """밝기 → 어두운 그라데이션 세기. 어두운 사진이면 0(그대로 잘 보인다)."""
    if luma <= SCRIM_DARK_LUMA:
        return 0.0
    span = (luma - SCRIM_DARK_LUMA) / (SCRIM_BRIGHT_LUMA - SCRIM_DARK_LUMA)
    return min(1.0, span) * SCRIM_MAX


def _text_width(draw: ImageDraw.ImageDraw, text: str, font, tracking: float) -> float:
    return draw.textlength(text, font=font) + tracking * max(0, len(text) - 1)


def _draw_tracked(draw: ImageDraw.ImageDraw, xy, text: str, font, fill, tracking: float) -> None:
    """자간을 벌려 가운데 정렬로 그린다 — Pillow 에 자간 옵션이 없어 한 글자씩 찍는다."""
    x = xy[0] - _text_width(draw, text, font, tracking) / 2
    for char in text:
        draw.text((x, xy[1]), char, font=font, fill=fill, anchor="lm")
        x += draw.textlength(char, font=font) + tracking


def _draw_title(image: Image.Image, title: str) -> None:
    """상단에 흰 제목을 얹는다(제자리 수정). 폰트를 못 찾으면 아무것도 안 그린다."""
    font_path = _find_font_path()
    if font_path is None:
        logger.warning("표지 제목용 한글 폰트를 찾지 못해 제목을 생략합니다.")
        return

    band = round(image.height * TITLE_BAND)
    strength = _scrim_strength(_band_luma(image, band))
    if strength > 0:
        shade = _gradient(image.width, band, strength, top_dark=True)
        image.paste(Image.new("RGB", (image.width, band), (8, 14, 24)), (0, 0), shade)

    size = round(image.width * TITLE_SIZE)
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(font_path, size)
    # 긴 제목은 폭 84% 안에 들어올 때까지 줄인다(너무 작아지면 포기하고 그대로 둔다).
    while size > round(image.width * 0.045) and _text_width(
        draw, title, font, size * TITLE_TRACKING
    ) > image.width * 0.84:
        size -= 2
        font = ImageFont.truetype(font_path, size)

    center = (image.width / 2, band * 0.52)
    tracking = size * TITLE_TRACKING
    # 그림자는 그라데이션이 0일 때(어두운 사진)도 글자 경계를 살려 준다.
    _draw_tracked(draw, (center[0] + 2, center[1] + 2), title, font, (0, 0, 0, 120), tracking)
    _draw_tracked(draw, center, title, font, (255, 255, 255), tracking)


def build_cover(image_bytes: bytes, title: str | None = None) -> bytes | None:
    """대표 사진 바이트 → 보정·제목을 얹은 표지 JPEG 바이트. 실패하면 None.

    title 이 비어 있으면 사진만 보정해 돌려준다(제목 없는 릴스가 있다).
    표지는 부가 정보라 여기서 죽으면 안 된다 — 예외를 삼키고 None 을 주면
    호출 측이 완성 영상에서 프레임을 뽑는 기존 경로로 돌아간다.
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as raw:
            image = (ImageOps.exif_transpose(raw) or raw).convert("RGB")
            image = ImageOps.fit(
                image, (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5)
            )
        image = _cool_filter(image)

        name = (title or "").strip()
        if name:
            _draw_title(image, name)
        else:
            # 제목이 없으면 어둡게 깔 이유가 없다 — 상단을 살짝 밝혀 청량함만 더한다.
            band = round(HEIGHT * 0.22)
            glow = _gradient(WIDTH, band, TOP_GLOW, top_dark=True)
            image.paste(Image.new("RGB", (WIDTH, band), (255, 255, 255)), (0, 0), glow)

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88, optimize=True)
        return buffer.getvalue()
    except Exception:
        logger.warning("릴스 표지 생성 실패(무시)", exc_info=True)
        return None
