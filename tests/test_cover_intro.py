"""표지 인트로 합성 자체 점검 — `python tests/test_cover_intro.py`.

실제로 ffmpeg 을 돌려 본편 앞에 2초가 붙는지, 붙인 뒤에도 **본편 재생 속도가
틀어지지 않는지**(타임스케일이 어긋나면 그렇게 된다) 확인한다.
프레임워크 없음 — 깨지면 assert 로 죽는다. ffmpeg 이 없으면 건너뛴다.
"""
import io
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from services import video_service
from utils import cover_image

MAIN_SECONDS = 3.0
TOLERANCE = 0.25  # concat 은 키프레임 경계라 딱 떨어지지 않는다


def _make_photo(path: Path, color=(60, 120, 190)) -> None:
    Image.new("RGB", (1200, 800), color).save(path, format="JPEG", quality=95)


def _make_video(path: Path, seconds: float, with_audio: bool) -> None:
    """테스트용 mp4 — 본편/대표 영상 양쪽에 쓴다."""
    args = ["-f", "lavfi", "-t", f"{seconds:.3f}", "-i",
            f"testsrc=size=1080x1920:rate=30"]
    if with_audio:
        args += ["-f", "lavfi", "-t", f"{seconds:.3f}", "-i", "anullsrc=r=44100:cl=stereo",
                 "-c:a", "aac", "-b:a", "128k"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
             "-profile:v", "high", "-pix_fmt", "yuv420p", str(path)]
    video_service._run_ffmpeg(args)


def _duration(path: Path) -> float:
    return float(video_service._ffprobe_video(path)["duration"])


def _run_case(label: str, cover_is_video: bool, with_audio: bool, title: str | None) -> None:
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        main = work / "main.mp4"
        _make_video(main, MAIN_SECONDS, with_audio)
        before = _duration(main)

        if cover_is_video:
            source = work / "cover_clip.mp4"
            _make_video(source, 5.0, with_audio=False)
        else:
            source = work / video_service.COVER_SOURCE_NAME
            _make_photo(source)

        job = {"intro_source": str(source), "title": title, "reels_idx": 1}
        video_service._prepend_cover_intro(main, job)

        after = _duration(main)
        grew = after - before
        assert abs(grew - video_service.INTRO_SECONDS) < TOLERANCE, (
            f"{label}: 2초가 안 붙었다 ({before:.2f} → {after:.2f}, +{grew:.2f})"
        )
        info = video_service._ffprobe_video(main)
        assert int(info["width"]) == 1080 and int(info["height"]) == 1920, info
        assert bool(info["has_audio"]) is with_audio, (
            f"{label}: 오디오 트랙이 {'사라졌다' if with_audio else '생겼다'}"
        )
        print(f"  ok  {label} ({before:.2f} → {after:.2f}초)")


def test_photo_cover_intro():
    _run_case("사진 대표 + BGM", cover_is_video=False, with_audio=True, title="동해")


def test_photo_cover_no_audio():
    _run_case("사진 대표 + 무음 본편", cover_is_video=False, with_audio=False, title="동해")


def test_video_cover_intro():
    _run_case("영상 대표 + BGM", cover_is_video=True, with_audio=True, title="동해")


def test_no_title_still_works():
    """제목이 없어도 인트로는 붙는다 (사진만 2초)."""
    _run_case("제목 없음", cover_is_video=False, with_audio=True, title=None)


def test_missing_source_is_noop():
    """인트로 원본이 없으면 아무것도 하지 않는다 — 본편 길이가 그대로여야 한다."""
    with tempfile.TemporaryDirectory() as temp:
        main = Path(temp) / "main.mp4"
        _make_video(main, MAIN_SECONDS, with_audio=False)
        before = _duration(main)
        video_service._prepend_cover_intro(main, {"intro_source": None, "title": "동해"})
        video_service._prepend_cover_intro(
            main, {"intro_source": str(Path(temp) / "없는파일.jpg"), "title": "동해"}
        )
        assert abs(_duration(main) - before) < 0.01, "원본이 없는데 본편이 바뀌었다"
        print("  ok  원본 없음 → 무동작")


def test_title_overlay_is_transparent_png():
    """제목 오버레이는 전체 크기 RGBA PNG 이고 아래쪽은 완전히 투명하다."""
    data = cover_image.build_title_overlay(1080, 1920, "동해")
    if data is None:
        print("[skip] 한글 폰트 없음 — 오버레이 검증 생략")
        return
    with Image.open(io.BytesIO(data)) as overlay:
        assert overlay.mode == "RGBA" and overlay.size == (1080, 1920), (overlay.mode, overlay.size)
        bottom = overlay.crop((0, 1200, 1080, 1920)).getchannel("A")
        assert bottom.getextrema() == (0, 0), "제목 띠 아래가 투명하지 않다 — 영상을 가린다"
    assert cover_image.build_title_overlay(1080, 1920, "  ") is None, "빈 제목은 None 이어야"
    print("  ok  제목 오버레이")


if __name__ == "__main__":
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("[skip] ffmpeg/ffprobe 없음 — 인트로 점검 생략")
        raise SystemExit(0)
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
    print("모두 통과")
