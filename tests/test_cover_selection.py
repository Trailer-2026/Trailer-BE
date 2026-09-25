# -*- coding: utf-8 -*-
"""대표 사진 선택(표지·인트로) 자체 점검 — `python tests/test_cover_selection.py`.

네 렌더 입구가 각각 다른 방식으로 대표를 고르는데, 그 결과가 표지(썸네일)와 인트로
원본으로 제대로 이어지는지 본다. DB·다운로드·렌더 서브프로세스는 스텁이다.

지키려는 규칙:
- photos-only / photos-ordered: cover_index 는 **업로드 순서 1부터**, 안 주면 1번
- 범위 밖 번호는 400 (조용히 1번으로 떨어지지 않는다)
- GPS 가 없어 영상에서 빠지는 사진도 표지로는 쓴다
- 대표가 영상이면 썸네일은 첫 프레임, 인트로 원본은 그 클립 자체
- promo: cover_index 는 **지점 번호**. 이미지 없는 지점이면 표지 없음
- travel: 사용자가 고를 수 없어 무작위 — 단 내가 올린 사진을 관광 대표 이미지보다 우선
- 표지를 만든 렌더는 TRAILER 인트로(--intro)를 붙이지 않는다
"""
import io
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from core.exceptions.custom import BadRequestException
from schemas.video_schema import PromoPoint, PromoRenderRequest
from services import video_service
from utils import tour_place

SEOUL, DAEJEON, BUSAN, JEJU = (
    (37.5665, 126.978), (36.3504, 127.3845), (35.1152, 129.0423), (33.4996, 126.5312),
)
# 어느 사진이 표지가 됐는지 색으로 가린다. 청량 보정이 색을 살짝 밀어도 순서는 안 바뀐다.
COLORS = {"red": (220, 40, 40), "green": (40, 200, 60), "blue": (40, 70, 220)}


# --------------------------------------------------------------------------- #
# 입력 만들기
# --------------------------------------------------------------------------- #
def _jpeg(color: tuple[int, int, int], gps: tuple[float, float] | None) -> bytes:
    image = Image.new("RGB", (1200, 800), color)
    exif = image.getexif()
    if gps is not None:
        gps_ifd = exif.get_ifd(0x8825)
        gps_ifd[1], gps_ifd[2] = "N", (float(int(gps[0])), 0.0, 0.0)
        gps_ifd[3], gps_ifd[4] = "E", (float(int(gps[1])), 0.0, 0.0)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90, exif=exif)
    return buffer.getvalue()


def _mp4(path: Path, seconds: float = 2.0) -> bytes:
    video_service._run_ffmpeg([
        "-f", "lavfi", "-t", f"{seconds:.2f}", "-i", "testsrc=size=320x568:rate=15",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path),
    ])
    return path.read_bytes()


def _uploads(items: list[tuple[str, bytes]]):
    return [(name, io.BytesIO(content)) for name, content in items]


def _dominant(path: Path) -> str:
    """표지 이미지의 우세 채널 → 어느 사진이 뽑혔는지."""
    red, green, blue = Image.open(path).convert("RGB").resize((1, 1)).getpixel((0, 0))
    return {red: "red", green: "green", blue: "blue"}[max(red, green, blue)]


# --------------------------------------------------------------------------- #
# 실행 (DB·렌더는 스텁)
# --------------------------------------------------------------------------- #
def _run_photos(items, **kwargs) -> dict:
    captured = {}

    def fake_spawn(db, travel_data_path, bgm_path, theme, user_idx, title=None, **extra):
        captured["job_dir"] = Path(travel_data_path).parent
        captured["travel_data"] = Path(travel_data_path)
        captured.update(extra)
        return {"reels_idx": 1}

    originals = (video_service._spawn_render_job, video_service._region_of_trip,
                 video_service._name_stops)
    video_service._spawn_render_job = fake_spawn
    video_service._region_of_trip = lambda points: None       # 카카오 호출 차단
    video_service._name_stops = lambda track, media: None
    try:
        video_service.start_render_photos_only(None, _uploads(items), user_idx=1, **kwargs)
    finally:
        (video_service._spawn_render_job, video_service._region_of_trip,
         video_service._name_stops) = originals
    return captured


def _run_promo(points, cover_index=None, images=None) -> dict:
    captured, images = {}, images or {}

    def fake_spawn(db, travel_data_path, bgm_path, theme, user_idx, title=None, **extra):
        captured["job_dir"] = Path(travel_data_path).parent
        captured.update(extra)
        return {"reels_idx": 1}

    originals = (video_service._spawn_render_job, video_service._fetch_travel_image,
                 video_service._region_of_trip, tour_place.image_near)
    video_service._spawn_render_job = fake_spawn
    video_service._fetch_travel_image = lambda url: images.get(url)
    video_service._region_of_trip = lambda points_: "부산"
    tour_place.image_near = lambda lat, lng: None
    try:
        video_service.start_render_promo(None, 1, PromoRenderRequest(
            title="코스", cover_index=cover_index,
            points=[PromoPoint(name=n, latitude=la, longitude=lo, image_url=u)
                    for n, (la, lo), u in points],
        ))
    finally:
        (video_service._spawn_render_job, video_service._fetch_travel_image,
         video_service._region_of_trip, tour_place.image_near) = originals
    return captured


def _run_travel(schedules, images, downloads) -> dict:
    captured = {}

    def fake_spawn(db, travel_data_path, bgm_path, theme, user_idx, title=None, **extra):
        captured["job_dir"] = Path(travel_data_path).parent
        captured.update(extra)
        return {"reels_idx": 1}

    originals = (video_service._spawn_render_job, video_service._fetch_travel_image,
                 video_service.travel_dao.get_by_idx, video_service.schedule_dao.list_by_travel,
                 video_service.travel_image_dao.by_travel, video_service._region_of_trip,
                 video_service._name_stops, tour_place.image_near)
    video_service._spawn_render_job = fake_spawn
    video_service._fetch_travel_image = lambda url: downloads.get(url)
    video_service.travel_dao.get_by_idx = lambda db, idx: SimpleNamespace(
        user_idx=1, title="동해 여행", region="강원")
    video_service.schedule_dao.list_by_travel = lambda db, idx: schedules
    video_service.travel_image_dao.by_travel = lambda db, idx: images
    video_service._region_of_trip = lambda points: "강원"
    video_service._name_stops = lambda track, media: None
    tour_place.image_near = lambda lat, lng: None
    try:
        video_service.start_render_travel(None, SimpleNamespace(user_idx=1), 1)
    finally:
        (video_service._spawn_render_job, video_service._fetch_travel_image,
         video_service.travel_dao.get_by_idx, video_service.schedule_dao.list_by_travel,
         video_service.travel_image_dao.by_travel, video_service._region_of_trip,
         video_service._name_stops, tour_place.image_near) = originals
    return captured


# --------------------------------------------------------------------------- #
# 점검
# --------------------------------------------------------------------------- #
JOBS: list[Path] = []


def _keep(captured: dict) -> dict:
    JOBS.append(captured["job_dir"])
    return captured


def test_photos_default_is_first():
    """번호를 안 주면 1번(첫 번째로 보낸 파일)이 표지다."""
    items = [("a.jpg", _jpeg(COLORS["red"], SEOUL)),
             ("b.jpg", _jpeg(COLORS["green"], BUSAN)),
             ("c.jpg", _jpeg(COLORS["blue"], JEJU))]
    got = _keep(_run_photos(items, title="여행"))
    assert _dominant(Path(got["cover_path"])) == "red", "1번이 아니다"
    assert Path(got["intro_source"]).name == video_service.COVER_SOURCE_NAME
    print("  ok  미지정 → 1번")


def test_photos_index_picks_that_file():
    """cover_index 는 업로드 순서 1부터다."""
    items = [("a.jpg", _jpeg(COLORS["red"], SEOUL)),
             ("b.jpg", _jpeg(COLORS["green"], BUSAN)),
             ("c.jpg", _jpeg(COLORS["blue"], JEJU))]
    for index, expected in ((1, "red"), (2, "green"), (3, "blue")):
        got = _keep(_run_photos(items, title="여행", cover_index=index))
        assert _dominant(Path(got["cover_path"])) == expected, f"{index}번이 {expected} 이어야"
    print("  ok  1/2/3번 각각 선택")


def test_photos_ordered_mode_same():
    """업로드 순서 모드(photos-ordered)도 대표 선택 규칙이 같다."""
    items = [("a.jpg", _jpeg(COLORS["red"], SEOUL)),
             ("b.jpg", _jpeg(COLORS["green"], BUSAN))]
    got = _keep(_run_photos(items, title="여행", cover_index=2, sort_by_time=False))
    assert _dominant(Path(got["cover_path"])) == "green"
    print("  ok  photos-ordered 동일")


def test_photos_index_out_of_range():
    """범위 밖 번호는 400 — 조용히 1번으로 떨어지지 않는다."""
    items = [("a.jpg", _jpeg(COLORS["red"], SEOUL)),
             ("b.jpg", _jpeg(COLORS["green"], BUSAN))]
    for bad in (0, 3, 99, -1):
        try:
            _keep(_run_photos(items, title="여행", cover_index=bad))
        except BadRequestException:
            continue
        raise AssertionError(f"cover_index={bad} 가 통과했다")
    print("  ok  범위 밖 → 400")


def test_cover_may_have_no_gps():
    """GPS 가 없어 영상엔 못 들어가는 사진도 표지로는 쓴다."""
    items = [("nogps.jpg", _jpeg(COLORS["green"], None)),   # 대표지만 좌표 없음
             ("b.jpg", _jpeg(COLORS["red"], SEOUL)),
             ("c.jpg", _jpeg(COLORS["blue"], BUSAN))]
    got = _keep(_run_photos(items, title="여행", cover_index=1))
    assert _dominant(Path(got["cover_path"])) == "green", "GPS 없는 대표가 표지가 안 됐다"
    stops = len(__import__("json").loads(got["travel_data"].read_text(encoding="utf-8"))["mediaPoints"])
    assert stops == 2, f"GPS 없는 사진이 지점으로 들어갔다 ({stops})"
    print("  ok  GPS 없는 사진도 표지로")


def test_cover_video_uses_clip_for_intro():
    """대표가 영상이면 썸네일은 첫 프레임, 인트로 원본은 그 클립이다."""
    temp = video_service.UPLOADS_DIR / "_t"
    temp.mkdir(parents=True, exist_ok=True)
    try:
        clip = _mp4(temp / "c.mp4")
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    items = [("clip.mp4", clip),
             ("b.jpg", _jpeg(COLORS["red"], SEOUL)),
             ("c.jpg", _jpeg(COLORS["blue"], BUSAN))]
    got = _keep(_run_photos(items, title="여행", cover_index=1))
    assert got["cover_path"] and Path(got["cover_path"]).exists(), "썸네일이 없다"
    intro = Path(got["intro_source"])
    assert intro.suffix.lower() == ".mp4", f"인트로 원본이 클립이 아니다: {intro.name}"
    assert intro.exists(), "인트로로 쓸 클립이 사라졌다"
    print("  ok  영상 대표 → 썸네일=첫 프레임, 인트로=클립")


def test_promo_index_picks_point_image():
    """promo 의 cover_index 는 지점 번호다."""
    points = [("A", SEOUL, "http://a"), ("B", BUSAN, "http://b"), ("C", JEJU, "http://c")]
    images = {"http://a": _jpeg(COLORS["red"], None),
              "http://b": _jpeg(COLORS["green"], None),
              "http://c": _jpeg(COLORS["blue"], None)}
    for index, expected in ((1, "red"), (2, "green"), (3, "blue")):
        got = _keep(_run_promo(points, cover_index=index, images=images))
        assert _dominant(Path(got["cover_path"])) == expected, f"지점 {index}"
    print("  ok  promo 지점 번호")


def test_promo_missing_image_keeps_trailer():
    """이미지가 없는 지점을 지목하면 표지가 없고 TRAILER 인트로가 남는다."""
    points = [("A", SEOUL, None), ("B", BUSAN, "http://b"), ("C", JEJU, "http://c")]
    images = {"http://b": _jpeg(COLORS["green"], None), "http://c": _jpeg(COLORS["blue"], None)}
    got = _keep(_run_promo(points, cover_index=1, images=images))
    assert got["cover_path"] is None and got["intro_source"] is None
    print("  ok  promo 이미지 없는 지점 → 표지 없음")


def test_travel_prefers_my_photos():
    """travel 은 무작위지만 내가 올린 사진을 관광 대표 이미지보다 먼저 본다."""
    schedules = [
        SimpleNamespace(schedule_idx=1, kind="visit", title="1", latitude=SEOUL[0],
                        longitude=SEOUL[1], image_url=None),
        SimpleNamespace(schedule_idx=2, kind="visit", title="2", latitude=BUSAN[0],
                        longitude=BUSAN[1], image_url="http://tour/b.jpg"),
    ]
    images = [SimpleNamespace(image_idx=10, url="http://mine/1.jpg", schedule_idx=1)]
    downloads = {"http://mine/1.jpg": _jpeg(COLORS["red"], None),
                 "http://tour/b.jpg": _jpeg(COLORS["blue"], None)}
    # 무작위라 여러 번 돌려 **한 번도** 관광 이미지가 안 뽑히는지 본다.
    for _ in range(12):
        got = _keep(_run_travel(schedules, images, downloads))
        assert _dominant(Path(got["cover_path"])) == "red", "관광 이미지가 표지로 뽑혔다"
        assert Path(got["intro_source"]).name == video_service.COVER_SOURCE_NAME
    print("  ok  travel 내 사진 우선 (12회)")


def test_travel_falls_back_to_tour_image():
    """내 사진이 하나도 없으면 관광 대표 이미지로 표지를 만든다."""
    schedules = [
        SimpleNamespace(schedule_idx=1, kind="visit", title="1", latitude=SEOUL[0],
                        longitude=SEOUL[1], image_url="http://tour/a.jpg"),
        SimpleNamespace(schedule_idx=2, kind="visit", title="2", latitude=BUSAN[0],
                        longitude=BUSAN[1], image_url="http://tour/b.jpg"),
    ]
    downloads = {"http://tour/a.jpg": _jpeg(COLORS["blue"], None),
                 "http://tour/b.jpg": _jpeg(COLORS["blue"], None)}
    got = _keep(_run_travel(schedules, [], downloads))
    assert _dominant(Path(got["cover_path"])) == "blue"
    print("  ok  travel 폴백 → 관광 이미지")


def test_travel_random_spreads():
    """내 사진이 여럿이면 매번 같은 장을 고르지 않는다(무작위)."""
    schedules = [
        SimpleNamespace(schedule_idx=i, kind="visit", title=str(i), latitude=lat,
                        longitude=lng, image_url=None)
        for i, (lat, lng) in enumerate([SEOUL, DAEJEON, BUSAN], start=1)
    ]
    images = [SimpleNamespace(image_idx=10 + i, url=f"http://mine/{name}.jpg", schedule_idx=i)
              for i, name in enumerate(("red", "green", "blue"), start=1)]
    downloads = {f"http://mine/{name}.jpg": _jpeg(COLORS[name], None) for name in COLORS}
    seen = set()
    for _ in range(30):
        got = _keep(_run_travel(schedules, images, downloads))
        seen.add(_dominant(Path(got["cover_path"])))
    assert len(seen) >= 2, f"30번 돌렸는데 항상 같은 사진이다: {seen}"
    print(f"  ok  travel 무작위 (30회에 {len(seen)}종)")


def test_trailer_intro_switch():
    """표지가 있으면 --intro 를 빼고, 없으면 붙인다."""
    data = video_service.UPLOADS_DIR / "_c" / "travel_data.json"
    data.parent.mkdir(parents=True, exist_ok=True)
    try:
        with_cover = video_service._build_command(data, "default", trailer_intro=False)
        without = video_service._build_command(data, "default", trailer_intro=True)
    finally:
        shutil.rmtree(data.parent, ignore_errors=True)
    assert "--intro" not in with_cover, "표지가 있는데 TRAILER 도 붙는다"
    assert "--intro" in without, "표지가 없는데 TRAILER 가 안 붙는다"
    print("  ok  TRAILER 스위치")


if __name__ == "__main__":
    if shutil.which("ffmpeg") is None:
        print("[skip] ffmpeg 없음")
        raise SystemExit(0)
    try:
        for name, test in sorted(globals().items()):
            if name.startswith("test_"):
                test()
    finally:
        for job in JOBS:  # 스텁이라 렌더 스레드가 없다 — 여기서 치운다
            shutil.rmtree(job, ignore_errors=True)
    print("모두 통과")
