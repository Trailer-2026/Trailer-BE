# -*- coding: utf-8 -*-
"""사진 렌더 입력 상한 자체 점검 — `python tests/test_render_photo_limits.py`.

프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.
DB·네트워크·렌더 서브프로세스는 스텁으로 갈음한다.

지키려는 규칙: **장수·장당 크기 상한**, 사진을 통째로 메모리에 안 올림(한 장씩 capped read),
GPS 없는 사진은 디스크에도 안 남김, 실패하면 job 디렉터리를 남기지 않음,
그리고 이 전부를 거쳐도 촬영 시각 순 정렬은 그대로 동작함.
"""
import io
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from core.exceptions.custom import BadRequestException
from services import video_service


def _jpeg(lat: float | None = None, lng: float | None = None, taken: str | None = None) -> bytes:
    """GPS/촬영시각 EXIF 를 심은(또는 안 심은) 최소 JPEG 바이트."""
    buf = io.BytesIO()
    image = Image.new("RGB", (8, 8))
    exif = Image.Exif()
    if lat is not None:
        def dms(v):
            d = int(abs(v))
            m = int((abs(v) - d) * 60)
            return (float(d), float(m), round((abs(v) - d - m / 60) * 3600, 2))
        exif.get_ifd(0x8825).update({
            1: "N" if lat >= 0 else "S", 2: dms(lat),
            3: "E" if lng >= 0 else "W", 4: dms(lng),
        })
    if taken is not None:
        exif.get_ifd(0x8769)[0x9003] = taken
    image.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


class _Stream(io.BytesIO):
    """read(n) 호출을 기록하는 스트림 — 한 번에 통째로 읽지 않는지 확인용."""

    def __init__(self, data: bytes):
        super().__init__(data)
        self.read_sizes: list[int | None] = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


def _photos(*payloads: bytes) -> list[tuple[str, _Stream]]:
    return [(f"p{i}.jpg", _Stream(data)) for i, data in enumerate(payloads)]


def _job_dirs() -> set[Path]:
    root = video_service.UPLOADS_DIR
    return set(root.iterdir()) if root.exists() else set()


def _run(photos, **kwargs):
    """스텁을 걸고 start_render_photos_only 를 돌린다 → 렌더에 넘어간 travel_data(dict)."""
    captured = {}

    def fake_spawn(db, travel_data_path, bgm_path, theme, user_idx, title=None, region=None):
        captured["data"] = json.loads(Path(travel_data_path).read_text(encoding="utf-8"))
        captured["job_dir"] = Path(travel_data_path).parent
        return {"reels_idx": 1}

    original = (video_service._spawn_render_job, video_service._region_of_trip)
    video_service._spawn_render_job = fake_spawn
    video_service._region_of_trip = lambda points: None  # 카카오 호출 차단
    try:
        video_service.start_render_photos_only(None, photos, user_idx=1, **kwargs)
    finally:
        video_service._spawn_render_job, video_service._region_of_trip = original
    return captured


def _rejects(photos, expect: str, **kwargs) -> None:
    """400 이 나고 job 디렉터리가 남지 않는지 — 실패 경로의 뒷정리까지 함께 본다."""
    before = _job_dirs()
    try:
        _run(photos, **kwargs)
    except BadRequestException as e:
        assert expect in e.message, f"기대 문구 '{expect}' 없음: {e.message}"
    else:
        raise AssertionError(f"400 이 나야 한다: {expect}")
    assert _job_dirs() == before, "실패했는데 job 디렉터리가 남았다"


def main() -> None:
    seoul, busan = (37.5665, 126.9780), (35.1152, 129.0423)

    # 1) 장수 상한 — 초과분은 **읽기 전에** 걸러야 하므로 스트림을 한 번도 안 읽는다.
    over = _photos(*[_jpeg(*seoul)] * (video_service.MAX_RENDER_PHOTOS + 1))
    _rejects(over, "장까지 올릴 수 있습니다")
    assert all(not s.read_sizes for _, s in over), "상한 초과인데 사진을 읽었다"

    # 2) 장당 크기 상한 — 상한+1 바이트까지만 읽고 끊는다(초과분을 메모리에 안 올린다).
    huge = _photos(_jpeg(*seoul), b"x" * (video_service.MAX_RENDER_PHOTO_BYTES + 10))
    _rejects(huge, "MB 이하만 가능합니다")
    assert huge[1][1].read_sizes == [video_service.MAX_RENDER_PHOTO_BYTES + 1], huge[1][1].read_sizes

    # 3) 2장 미만 / GPS 있는 사진 2장 미만
    _rejects(_photos(_jpeg(*seoul)), "2장 이상 필요합니다")
    _rejects(_photos(_jpeg(*seoul), _jpeg()), "GPS 정보가 있는 사진이 2장")

    # 4) 사진이 모두 같은 장소면 이동 경로가 안 나온다(이 실패에서도 디렉터리는 안 남는다).
    _rejects(_photos(_jpeg(*seoul), _jpeg(*seoul)), "같은 장소라")

    # 5) 정상 경로: 촬영 시각 순으로 정렬되고, GPS 없는 사진은 디스크에도 안 남는다.
    #    (업로드 순서는 부산 → 서울, 촬영 시각은 서울이 먼저 → 서울이 앞에 와야 한다.)
    photos = _photos(
        _jpeg(*busan, taken="2026:08:01 18:00:00"),
        _jpeg(),  # GPS 없음 → 제외
        _jpeg(*seoul, taken="2026:08:01 09:00:00"),
    )
    # 성공 경로의 job 디렉터리는 렌더 스레드 몫이라 여기서 치운다(스텁이라 스레드가 없다).
    # 만든 디렉터리만 지운다 — UPLOADS_DIR 은 앱과 공유라 diff 로 쓸면 남의 것도 날아간다.
    jobs = []
    try:
        captured = _run(photos)
        jobs.append(captured["job_dir"])
        data = captured["data"]
        lat0 = data["trackPoints"][0]["latitude"]
        assert len(data["trackPoints"]) == 2, data["trackPoints"]
        assert abs(lat0 - seoul[0]) < 0.01, f"촬영 시각 순(서울 먼저)이어야: {lat0}"
        saved = sorted(p.name for p in captured["job_dir"].iterdir())
        assert saved == ["photo_0.jpg", "photo_2.jpg", "travel_data.json"], saved
        assert all(s.read_sizes for _, s in photos), "스트림을 안 읽었다"

        # 6) 순서 지정 모드는 촬영 시각을 무시하고 업로드 순서(부산 먼저)를 그대로 쓴다.
        ordered = _run(
            _photos(
                _jpeg(*busan, taken="2026:08:01 18:00:00"),
                _jpeg(*seoul, taken="2026:08:01 09:00:00"),
            ),
            sort_by_time=False,
        )
        jobs.append(ordered["job_dir"])
        lat0 = ordered["data"]["trackPoints"][0]["latitude"]
        assert abs(lat0 - busan[0]) < 0.01, f"업로드 순서(부산 먼저)여야: {lat0}"
    finally:
        for path in jobs:
            shutil.rmtree(path, ignore_errors=True)
    print("OK: 사진 렌더 입력 상한 자체 점검 통과")


if __name__ == "__main__":
    main()
