# -*- coding: utf-8 -*-
"""여행 렌더의 사진별 장소명 자체 점검 — `python tests/test_render_photo_labels.py`.

프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.
DB·TourAPI·카카오·이미지 다운로드·렌더 서브프로세스는 전부 스텁이다.

지키려는 규칙: 영상의 사진 위에는 **그 사진이 속한 일정 이름**이 뜬다.
- 1km 안의 일정이 한 지점으로 묶여도 사진마다 자기 일정 이름 (경복궁 → 국립민속박물관)
- 어느 일정에도 안 붙은 사진은 마지막 지점에 몰리지만 이름은 빈 값(라벨 숨김)
- 제목 없는 일정의 사진은 표에 안 적혀 지점 이름(좌표로 채운 이름)을 쓴다
- 일정에서 OFF_COURSE_KM 넘게 떨어져 찍은 사진(코스에 없는 바다)은 그 일정 이름을 달지 않고,
  일정 바로 다음 경유 지점이 되어 사진 위치로 찾은 이름을 쓴다
- 붙은 사진이 전부 다른 곳에서 찍힌 일정은 안 간 것으로 보고 뺀다(사진 없는 일정은 남김).
  빼면 경로가 안 그려질 때는 코스를 유지한다. 기차 일정은 코스 밖 판정에서 제외
- 사진 상한은 코스 일정부터 채우고, 사진이 다 잘린 경유지는 경로에서 빠진다
- 제목을 안 주면 릴스 제목은 여행 이름이다(일정 제목이 새면 안 된다)
"""
import io
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from services import video_service
from utils import kakao_local, tour_place


def _jpeg(lat: float, lng: float, taken: str | None = None) -> bytes:
    """GPS(와 촬영 시각) EXIF 를 심은 최소 JPEG."""
    def dms(v):
        d = int(abs(v))
        m = int((abs(v) - d) * 60)
        return (float(d), float(m), round((abs(v) - d - m / 60) * 3600, 2))

    exif = Image.Exif()
    exif.get_ifd(0x8825).update({1: "N", 2: dms(lat), 3: "E", 4: dms(lng)})
    if taken:
        exif.get_ifd(0x8769)[0x9003] = taken
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def _run(schedules, images, contents=None):
    contents = contents or {}
    captured = {}

    def fake_spawn(db, travel_data_path, *args, **kwargs):
        captured["data"] = json.loads(Path(travel_data_path).read_text(encoding="utf-8"))
        captured["job_dir"] = Path(travel_data_path).parent
        captured["title"] = args[3] if len(args) > 3 else kwargs.get("title")
        # job 디렉터리는 아래 finally 에서 지우므로 사진 바이트를 미리 읽어 둔다(순서 확인용).
        root = video_service.VIDEO_MAKER_DIR
        captured["bytes"] = {
            rel: (root / rel).read_bytes()
            for point in captured["data"]["mediaPoints"] for rel in point["photos"]
        }
        return {"reels_idx": 1}

    originals = (
        video_service._spawn_render_job, video_service._fetch_travel_image,
        video_service.travel_dao.get_by_idx, video_service.schedule_dao.list_by_travel,
        video_service.travel_image_dao.by_travel, tour_place.image_near, kakao_local.place_name_of,
    )
    video_service._spawn_render_job = fake_spawn
    video_service._fetch_travel_image = lambda url: contents.get(url, b"x" + url.encode())
    video_service.travel_dao.get_by_idx = lambda db, idx: SimpleNamespace(
        user_idx=1, title="여행", region="서울",
    )
    video_service.schedule_dao.list_by_travel = lambda db, idx: schedules
    video_service.travel_image_dao.by_travel = lambda db, idx: images
    tour_place.image_near = lambda lat, lng: None
    kakao_local.place_name_of = lambda lat, lng, **_: (
        "을왕리해수욕장" if lat < 37.5 and lng < 126.6 else "좌표로 찾은 이름"
    )
    try:
        video_service.start_render_travel(None, SimpleNamespace(user_idx=1), 1)
    finally:
        (video_service._spawn_render_job, video_service._fetch_travel_image,
         video_service.travel_dao.get_by_idx, video_service.schedule_dao.list_by_travel,
         video_service.travel_image_dao.by_travel, tour_place.image_near,
         kakao_local.place_name_of) = originals
        shutil.rmtree(captured.get("job_dir", Path("/nonexistent")), ignore_errors=True)
    captured["data"]["_bytes"] = captured["bytes"]
    captured["data"]["_title"] = captured["title"]
    return captured["data"]


def main() -> None:
    def schedule(idx, lat, lng, title, kind="visit"):
        return SimpleNamespace(schedule_idx=idx, kind=kind, title=title,
                               latitude=lat, longitude=lng, image_url=None)

    def image(idx, schedule_idx):
        return SimpleNamespace(image_idx=idx, url=f"https://x/{idx}.jpg", schedule_idx=schedule_idx)

    # 경복궁·국립민속박물관은 500m 라 한 지점으로 묶이고, 부산은 따로. 7번 사진은 일정 없음.
    schedules = [
        schedule(1, 37.5796, 126.9770, "경복궁"),
        schedule(2, 37.5823, 126.9794, "국립민속박물관"),
        schedule(3, 35.1587, 129.1604, None),
    ]
    images = [image(1, 1), image(2, 2), image(3, 2), image(5, 3), image(7, None)]
    data = _run(schedules, images)

    points = data["mediaPoints"]
    assert len(points) == 2, points                     # 1km 병합은 그대로
    assert points[0]["name"] == "경복궁"                 # 지도 라벨은 지점 이름
    assert points[1]["name"] == "좌표로 찾은 이름"        # 제목 없는 일정 → 좌표 이름

    labels = data["photoLabels"]
    first = [labels.get(p) for p in points[0]["photos"]]
    assert first == ["경복궁", "국립민속박물관", "국립민속박물관"], first
    # 부산 지점: 제목 없는 일정 사진은 표에 없음(→ 지점 이름), 일정 없는 사진은 빈 이름
    second = [labels.get(p) for p in points[1]["photos"]]
    assert second == [None, ""], second

    # 인천공항 → 경복궁 코스인데 을왕리 바다(공항에서 약 8km)에서 찍은 사진 2장이 공항 일정에
    # 붙어 있다. 공항 근처(0.5km)에서 찍은 사진은 공항 사진으로 남는다.
    airport, eulwang = (37.4602, 126.4407), (37.4476, 126.3725)
    schedules = [schedule(1, *airport, "인천국제공항"), schedule(2, 37.5796, 126.9770, "경복궁")]
    images = [image(1, 1), image(2, 1), image(3, 1), image(4, 2)]
    contents = {
        "https://x/1.jpg": _jpeg(37.4630, 126.4440),                          # 공항 0.4km
        "https://x/2.jpg": _jpeg(*eulwang, taken="2026:09:20 15:00:00"),
        "https://x/3.jpg": _jpeg(eulwang[0] + 0.001, eulwang[1], taken="2026:09:20 14:00:00"),
    }
    data = _run(schedules, images, contents)
    points = data["mediaPoints"]
    names = [p["name"] for p in points]
    assert names == ["인천국제공항", "을왕리해수욕장", "경복궁"], names   # 공항 다음 경유 지점
    sea = data["trackPoints"][points[1]["trackIndex"]]
    assert abs(sea["latitude"] - eulwang[0]) < 0.01, sea                   # 실제 바다 좌표
    labels = data["photoLabels"]
    assert [labels.get(p) for p in points[0]["photos"]] == ["인천국제공항"]
    # 바다 사진엔 일정 이름이 안 붙는다(→ 지점 이름 '을왕리해수욕장'), 촬영 시각 순(14시 → 15시)
    assert [labels.get(p) for p in points[1]["photos"]] == [None, None]
    sea_bytes = [data["_bytes"][p] for p in points[1]["photos"]]
    assert sea_bytes == [contents["https://x/3.jpg"], contents["https://x/2.jpg"]]
    assert data["_title"] == "여행", data["_title"]   # 릴스 제목 = 여행 이름(마지막 일정 제목 아님)

    # 안 간 일정 빼기: 경복궁 일정 사진이 전부 을왕리에서 찍힘 → 경복궁 빠지고 을왕리 경유지.
    # 박물관은 사진이 없어 남는다(관광 이미지 폴백 자리). 부산은 그 자리 사진이 있어 남는다.
    busan = (35.1152, 129.0423)
    schedules = [schedule(1, 37.5796, 126.9770, "경복궁"), schedule(2, 36.35, 127.38, "대전 박물관"),
                 schedule(3, *busan, "부산역")]
    images = [image(1, 1), image(2, 1), image(3, 3)]
    contents = {"https://x/1.jpg": _jpeg(*eulwang), "https://x/2.jpg": _jpeg(*eulwang),
                "https://x/3.jpg": _jpeg(busan[0] + 0.001, busan[1])}
    names = [p["name"] for p in _run(schedules, images, contents)["mediaPoints"]]
    assert names == ["을왕리해수욕장", "대전 박물관", "부산역"], names

    # 빼면 경로가 안 될 때: 일정 2개인데 둘 다 사진이 같은 곳(을왕리)에서 → 코스 유지
    schedules = [schedule(1, 37.5796, 126.9770, "경복궁"), schedule(2, *busan, "부산역")]
    images = [image(1, 1), image(2, 2)]
    contents = {"https://x/1.jpg": _jpeg(*eulwang), "https://x/2.jpg": _jpeg(*eulwang)}
    names = [p["name"] for p in _run(schedules, images, contents)["mediaPoints"]]
    assert names[0] == "경복궁" and "부산역" in names, names

    # 기차 일정: 달리는 기차 안 사진(출발역에서 60km)은 경유지가 되지 않고 기차 일정도 안 빠진다
    schedules = [schedule(1, 37.5547, 126.9706, "KTX 101 서울→부산", kind="train"),
                 schedule(2, *busan, "부산역")]
    images = [image(1, 1)]
    contents = {"https://x/1.jpg": _jpeg(37.02, 127.1)}
    data = _run(schedules, images, contents)
    assert [p["name"] for p in data["mediaPoints"]] == ["KTX 101 서울→부산", "부산역"]
    assert len(data["mediaPoints"][0]["photos"]) == 1

    # 경유지가 많을 때: 코스 밖 18곳(각 1장) + 경복궁 사진 1장 → 경복궁 사진은 남고,
    # 경유지는 남는 14장만 받으며 사진이 잘린 경유지는 경로에서 빠진다
    far_spots = [(37.40 + i * 0.03, 126.40) for i in range(18)]
    schedules = [schedule(1, *airport, "인천국제공항"), schedule(2, 37.5796, 126.9770, "경복궁")]
    images = [image(i, 1) for i in range(18)] + [image(99, 2)]
    contents = {f"https://x/{i}.jpg": _jpeg(*c, f"2026:09:20 {6 + i // 2:02d}:{(i % 2) * 30:02d}:00")
                for i, c in enumerate(far_spots)}
    contents["https://x/99.jpg"] = _jpeg(37.5800, 126.9772)
    data = _run(schedules, images, contents)
    points = data["mediaPoints"]
    assert points[-1]["name"] == "경복궁" and len(points[-1]["photos"]) == 1, points[-1]
    assert all(p["photos"] for p in points), [len(p["photos"]) for p in points]   # 빈 경유지 없음
    assert sum(len(p["photos"]) for p in points) == video_service.MAX_TRAVEL_RENDER_PHOTOS
    assert [p["trackIndex"] for p in points] == list(range(len(points)))       # 번호 다시 매김
    assert len(data["trackPoints"]) == len(points)
    print("OK: 여행 렌더 사진별 장소명 자체 점검 통과")


if __name__ == "__main__":
    main()
