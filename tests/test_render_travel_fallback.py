# -*- coding: utf-8 -*-
"""여행 렌더의 관광 이미지 폴백·사진 상한 자체 점검 — `python tests/test_render_travel_fallback.py`.

프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.
DB·TourAPI·이미지 다운로드·렌더 서브프로세스는 전부 스텁이다.

지키려는 규칙:
- 내 사진이 있는 일정엔 관광 이미지가 안 붙는다(내 사진 우선)
- 내 사진이 없는 일정은 schedule.image_url(추천 코스)로, 그것도 없으면 좌표로 실시간
  조회(직접 만든 여행)한 대표 이미지 1장으로 메운다
- 기차 일정은 조회 대상이 아니다
- 다운로드·조회 실패는 사진 없는 지점으로 남고 렌더는 계속된다
- 지점당 PHOTOS_PER_STOP 장·전체 MAX_TRAVEL_RENDER_PHOTOS 장 — 한 바퀴씩 돌며 채운다
"""
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import video_service
from utils import tour_place


def _schedule(idx, lat, lng, kind="visit", image_url=None):
    return SimpleNamespace(
        schedule_idx=idx, kind=kind, title=f"일정{idx}",
        latitude=lat, longitude=lng, image_url=image_url,
    )


def _image(idx, url, schedule_idx):
    return SimpleNamespace(image_idx=idx, url=url, schedule_idx=schedule_idx)


def _run(schedules, images, *, near=None, dead=()):
    """스텁을 걸고 start_render_travel 을 돌린다 → (travel_data dict, image_near 호출 좌표들)."""
    captured, near_calls = {}, []
    near = near or {}

    def fake_spawn(db, travel_data_path, bgm_path, theme, user_idx, title=None, region=None):
        captured["data"] = json.loads(Path(travel_data_path).read_text(encoding="utf-8"))
        captured["job_dir"] = Path(travel_data_path).parent
        return {"reels_idx": 1}

    def fake_near(lat, lng):
        near_calls.append((lat, lng))
        return near.get((lat, lng))

    originals = (
        video_service._spawn_render_job, video_service._fetch_travel_image,
        video_service.travel_dao.get_by_idx, video_service.schedule_dao.list_by_travel,
        video_service.travel_image_dao.by_travel, tour_place.image_near,
    )
    video_service._spawn_render_job = fake_spawn
    video_service._fetch_travel_image = lambda url: None if url in dead else b"x" + url.encode()
    video_service.travel_dao.get_by_idx = lambda db, idx: SimpleNamespace(
        user_idx=1, title="여행", region="부산",
    )
    video_service.schedule_dao.list_by_travel = lambda db, idx: schedules
    video_service.travel_image_dao.by_travel = lambda db, idx: images
    tour_place.image_near = fake_near
    try:
        video_service.start_render_travel(None, SimpleNamespace(user_idx=1), 1)
    finally:
        (video_service._spawn_render_job, video_service._fetch_travel_image,
         video_service.travel_dao.get_by_idx, video_service.schedule_dao.list_by_travel,
         video_service.travel_image_dao.by_travel, tour_place.image_near) = originals
    return captured, near_calls


def _photos(data):
    return [p["photos"] for p in data["mediaPoints"]]


def main() -> None:
    jobs: list[Path] = []
    seoul, daejeon, busan, jeju = (37.5665, 126.978), (36.3504, 127.3845), (35.1152, 129.0423), (33.4996, 126.5312)
    try:
        # 1) 폴백 3종: 내 사진 있음(폴백 없음) / image_url(API 안 부름) / 좌표 조회 / 기차(제외)
        schedules = [
            _schedule(1, *seoul),                                   # 내 사진 1장 → 그대로
            _schedule(2, *daejeon, kind="train"),                   # 기차 → 조회 안 함, 사진 없음
            _schedule(3, *busan, image_url="http://tour/busan.jpg"),  # 추천 코스 → 저장값
            _schedule(4, *jeju),                                    # 직접 입력 → 실시간 조회
        ]
        images = [_image(10, "https://gcs/mine.jpg", 1)]
        captured, near_calls = _run(schedules, images, near={jeju: "http://tour/jeju.jpg"})
        jobs.append(captured["job_dir"])
        photos = _photos(captured["data"])
        assert [len(p) for p in photos] == [1, 0, 1, 1], photos
        assert near_calls == [jeju], f"좌표 조회는 image_url 도 내 사진도 없는 방문지만: {near_calls}"
        names = sorted(p.name for p in captured["job_dir"].iterdir())
        assert names == ["img_0.jpg", "img_1.jpg", "img_2.jpg", "travel_data.json"], names

        # 2) 실패는 조용히 사진 없는 지점 — 다운로드 실패(dead)·조회 결과 없음 둘 다
        captured, _ = _run(schedules, images, dead={"http://tour/busan.jpg"})
        jobs.append(captured["job_dir"])
        assert [len(p) for p in _photos(captured["data"])] == [1, 0, 0, 0], _photos(captured["data"])

        # 3) 상한 — 지점당 3장, 전체 15장, 한 바퀴씩. 지점 A 5장·B 0장(관광 1장)·C 4장 → 3/1/3.
        schedules = [_schedule(1, *seoul), _schedule(2, *daejeon, image_url="http://t/d.jpg"), _schedule(3, *busan)]
        images = [_image(i, f"https://gcs/a{i}.jpg", 1) for i in range(5)] + \
                 [_image(10 + i, f"https://gcs/c{i}.jpg", 3) for i in range(4)]
        captured, _ = _run(schedules, images)
        jobs.append(captured["job_dir"])
        photos = _photos(captured["data"])
        assert [len(p) for p in photos] == [3, 1, 3], photos
        assert photos[0] == [f"assets/uploads/{captured['job_dir'].name}/img_{i}.jpg" for i in range(3)], photos[0]

        # 4) 전체 상한이 지점당 상한보다 먼저 닿으면 뒤 바퀴가 잘린다 — 6지점 × 3장 = 18 > 15.
        #    1바퀴 6 + 2바퀴 6 = 12, 3바퀴는 앞 3지점만 → [3,3,3,2,2,2].
        pts = [seoul, daejeon, busan, jeju, (35.8714, 128.6014), (37.4563, 126.7052)]
        schedules = [_schedule(i + 1, *pt) for i, pt in enumerate(pts)]
        images = [_image(i * 10 + k, f"https://gcs/{i}_{k}.jpg", i + 1) for i in range(6) for k in range(3)]
        captured, _ = _run(schedules, images)
        jobs.append(captured["job_dir"])
        assert [len(p) for p in _photos(captured["data"])] == [3, 3, 3, 2, 2, 2], _photos(captured["data"])
        assert sum(len(p) for p in _photos(captured["data"])) == video_service.MAX_TRAVEL_RENDER_PHOTOS
    finally:
        for job in jobs:
            shutil.rmtree(job, ignore_errors=True)

    print("OK: 여행 렌더 관광 이미지 폴백·사진 상한 점검 통과")


if __name__ == "__main__":
    main()
