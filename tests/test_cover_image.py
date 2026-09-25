"""릴스 표지 생성 자체 점검 — `python tests/test_cover_image.py`.

프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.
검증 대상은 '깨지지 않는가'와 '흰 글씨가 묻히지 않게 밝기로 판단하는가' 둘이다.
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageStat

from utils import cover_image


def _photo(color: tuple[int, int, int], size=(1200, 800)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def _top_luma(image: Image.Image) -> float:
    band = round(image.height * cover_image.TITLE_BAND)
    return ImageStat.Stat(image.crop((0, 0, image.width, band)).convert("L")).mean[0]


def test_size_and_format():
    """가로 사진이든 세로 사진이든 9:16 표지 한 장이 나온다."""
    for size in ((1200, 800), (800, 1200), (900, 900)):
        result = cover_image.build_cover(_photo((120, 160, 200), size), "동해")
        assert result, f"표지를 만들지 못함: {size}"
        image = _open(result)
        assert image.size == (cover_image.WIDTH, cover_image.HEIGHT), image.size


def test_no_title_is_fine():
    """제목이 없으면 보정만 한다 — 예외 없이 이미지가 나와야 한다."""
    for title in (None, "", "   "):
        assert cover_image.build_cover(_photo((90, 140, 190)), title), f"제목={title!r}"


def test_filter_shifts_cool():
    """청량 보정은 파랑을 올리고 빨강을 내린다."""
    plain = _open(cover_image.build_cover(_photo((128, 128, 128)), None))
    red, green, blue = ImageStat.Stat(plain.crop((0, 500, 540, 900))).mean
    assert blue > red, f"파랑이 빨강보다 높아야 함 (R={red:.1f} B={blue:.1f})"


def test_bright_photo_gets_scrim():
    """밝은 사진은 제목 뒤를 어둡게 깔고, 어두운 사진은 그대로 둔다."""
    assert cover_image._scrim_strength(60) == 0.0
    assert cover_image._scrim_strength(255) == cover_image.SCRIM_MAX
    assert 0 < cover_image._scrim_strength(155) < cover_image.SCRIM_MAX

    bright_plain = _open(cover_image.build_cover(_photo((245, 248, 250)), None))
    bright_titled = _open(cover_image.build_cover(_photo((245, 248, 250)), "동해"))
    assert _top_luma(bright_titled) < _top_luma(bright_plain) - 10, (
        "밝은 사진인데 제목 영역이 어두워지지 않았다 "
        f"({_top_luma(bright_plain):.1f} → {_top_luma(bright_titled):.1f})"
    )


def test_long_title_shrinks_to_fit():
    """긴 제목도 폭 안에 들어온다 (폰트가 없는 환경이면 건너뛴다)."""
    if cover_image._find_font_path() is None:
        print("[skip] 한글 폰트 없음 — 제목 렌더 검증 생략")
        return
    dark = _photo((20, 30, 45))
    short = _open(cover_image.build_cover(dark, "동해"))
    long = _open(cover_image.build_cover(dark, "동해 1박2일 여행코스 이대로 다녀오세요"))
    band = round(cover_image.HEIGHT * cover_image.TITLE_BAND)
    for image, label in ((short, "짧은 제목"), (long, "긴 제목")):
        # 어두운 사진이므로 흰 글씨가 그려졌다면 상단에 아주 밝은 픽셀이 있어야 한다.
        assert image.crop((0, 0, image.width, band)).convert("L").getextrema()[1] > 200, (
            f"{label}: 흰 글씨가 그려지지 않았다"
        )
        # 글씨가 폭을 넘치면 좌우 끝까지 흰 픽셀이 닿는다 — 가장자리 열은 비어 있어야 한다.
        edge = image.crop((0, 0, 8, band)).convert("L")
        assert ImageStat.Stat(edge).mean[0] < 90, f"{label}: 글씨가 화면 밖으로 넘친다"


def test_huge_jpeg_is_downscaled_not_rejected():
    """고화소 JPEG 는 거부가 아니라 축소 디코드로 통과한다 (요즘 폰 사진이 48MP 다)."""
    huge = _photo((90, 150, 200), (8000, 6000))  # 48MP
    assert cover_image.build_cover(huge, "동해"), "48MP JPEG 가 거부됐다"
    # draft 가 먹었는지 — 상한을 아주 낮춰도 JPEG 는 통과해야 한다.
    original = cover_image.MAX_SOURCE_PIXELS
    cover_image.MAX_SOURCE_PIXELS = 4_000_000
    try:
        assert cover_image.build_cover(huge, "동해"), "draft 축소 디코드가 안 먹었다"
    finally:
        cover_image.MAX_SOURCE_PIXELS = original
    print("  ok  고화소 JPEG 축소 디코드")


def test_oversized_png_is_skipped():
    """draft 가 안 먹는 포맷의 과대 이미지는 표지를 포기한다 (메모리 보호)."""
    buffer = io.BytesIO()
    Image.new("RGB", (3000, 2000), (100, 120, 140)).save(buffer, format="PNG")
    original = cover_image.MAX_SOURCE_PIXELS
    cover_image.MAX_SOURCE_PIXELS = 1_000_000  # 6MP 짜리를 넘기게
    cover_image.logger.disabled = True
    try:
        assert cover_image.build_cover(buffer.getvalue(), "동해") is None, "상한을 넘겼는데 통과했다"
    finally:
        cover_image.MAX_SOURCE_PIXELS = original
        cover_image.logger.disabled = False
    print("  ok  과대 PNG → 생략(폴백)")


def test_garbage_bytes_return_none():
    """이미지가 아니면 None — 호출 측이 기존 ffmpeg 경로로 폴백한다."""
    cover_image.logger.disabled = True  # 여기선 경고 트레이스백이 기대된 동작이라 가린다
    try:
        assert cover_image.build_cover(b"not an image", "동해") is None
        assert cover_image.build_cover(b"", "동해") is None
    finally:
        cover_image.logger.disabled = False


if __name__ == "__main__":
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
            print(f"  ok  {name}")
    print("모두 통과")
