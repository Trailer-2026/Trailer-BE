# -*- coding: utf-8 -*-
"""홍보 영상 렌더 자체 점검 — `python tests/test_render_promo.py`.

프레임워크 없음 — 깨지면 assert 로 죽는다. DB·TourAPI·다운로드·렌더 서브프로세스는 스텁.

지키려는 규칙:
- 지점마다 image_url 이 있으면 그걸, 없으면 좌표 조회 결과 1장 (실패는 사진 없음)
- 1km 안 지점은 묶이고, 전부 같은 곳이면 400 + job 디렉터리 정리
- 렌더 명령에 30초 상한과 --max-chunks 1 이 같이 들어간다(상한이 조각마다 걸리므로)
- 스키마가 지점 수 2~6 을 막는다
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError

from core.exceptions.custom import BadRequestException
from schemas.video_schema import PromoRenderRequest
from services import video_service
from utils import tour_place


def _req(points, **kw):
    return PromoRenderRequest(title="부산 코스", points=points, **kw)


def _run(req, *, near=None, dead=()):
    captured = {}
    near = near or {}

    def fake_spawn(db, travel_data_path, bgm_path, theme, user_idx, title=None, region=None,
                   max_video_seconds=None):
        captured["data"] = json.loads(Path(travel_data_path).read_text(encoding="utf-8"))
        captured["job_dir"] = Path(travel_data_path).parent
        captured["max_video_seconds"] = max_video_seconds
        captured["title"], captured["region"] = title, region
        return {"reels_idx": 1}

    originals = (video_service._spawn_render_job, video_service._fetch_travel_image,
                 video_service._region_of_trip, tour_place.image_near)
    video_service._spawn_render_job = fake_spawn
    video_service._fetch_travel_image = lambda url: None if url in dead else b"\xff\xd8" + url.encode()
    video_service._region_of_trip = lambda pts: "지오코딩"
    tour_place.image_near = lambda lat, lng: near.get((lat, lng))
    try:
        video_service.start_render_promo(None, 1, req)
    finally:
        (video_service._spawn_render_job, video_service._fetch_travel_image,
         video_service._region_of_trip, tour_place.image_near) = originals
    return captured


def main() -> None:
    jobs: list[Path] = []
    haeundae, gwangan, taejongdae = (35.1587, 129.1604), (35.1532, 129.1186), (35.0517, 129.0873)
    try:
        # 1) 이미지 출처 3종: 직접 지정 / 좌표 조회 / 없음(조회 결과 None)
        req = _req([
            {"name": "해운대", **dict(zip(("latitude", "longitude"), haeundae)), "image_url": "https://my/h.jpg"},
            {"name": "광안리", **dict(zip(("latitude", "longitude"), gwangan))},
            {"name": "태종대", **dict(zip(("latitude", "longitude"), taejongdae))},
        ], region="부산")
        c = _run(req, near={gwangan: "http://tour/g.jpg"})
        jobs.append(c["job_dir"])
        photos = [p["photos"] for p in c["data"]["mediaPoints"]]
        assert [len(p) for p in photos] == [1, 1, 0], photos
        assert [p["name"] for p in c["data"]["mediaPoints"]] == ["해운대", "광안리", "태종대"]
        assert c["max_video_seconds"] == video_service.PROMO_VIDEO_SECONDS == 30.0
        assert (c["title"], c["region"]) == ("부산 코스", "부산")

        # 2) region 생략이면 역지오코딩, 다운로드 실패는 사진 없음
        c = _run(_req(req.points), dead={"https://my/h.jpg"})
        jobs.append(c["job_dir"])
        assert c["region"] == "지오코딩"
        assert [len(p["photos"]) for p in c["data"]["mediaPoints"]] == [0, 0, 0]

        # 3) 전부 같은 곳(1km 안) → 400, job 디렉터리 안 남음
        before = set(video_service.UPLOADS_DIR.iterdir()) if video_service.UPLOADS_DIR.exists() else set()
        same = _req([{"name": f"p{i}", "latitude": 35.1587, "longitude": 129.1604 + i * 0.001} for i in range(3)])
        try:
            _run(same)
        except BadRequestException as e:
            assert "같은 장소" in e.message, e.message
        else:
            raise AssertionError("400 이 나야 한다")
        after = set(video_service.UPLOADS_DIR.iterdir()) if video_service.UPLOADS_DIR.exists() else set()
        assert after == before, "실패했는데 job 디렉터리가 남았다"

        # 4) 렌더 명령: 상한을 주면 조각 분할을 끈다. 안 주면 둘 다 없다.
        cmd = video_service._build_command(video_service.VIDEO_MAKER_DIR / "x" / "t.json", "default", 30.0)
        assert cmd[cmd.index("--max-video-seconds") + 1] == "30.0", cmd
        assert cmd[cmd.index("--max-chunks") + 1] == "1", cmd
        plain = video_service._build_command(video_service.VIDEO_MAKER_DIR / "x" / "t.json", "default")
        assert "--max-video-seconds" not in plain and "--max-chunks" not in plain, plain

        # 5) 스키마: 지점 1개 / 7개는 422 감
        for n in (1, 7):
            try:
                _req([{"name": f"p{i}", "latitude": 35.0 + i, "longitude": 129.0} for i in range(n)])
            except ValidationError:
                pass
            else:
                raise AssertionError(f"지점 {n}개는 거부돼야 한다")
    finally:
        for job in jobs:
            shutil.rmtree(job, ignore_errors=True)

    _check_image_fetch_size_limit()
    print("OK: 홍보 영상 렌더 점검 통과")


def _check_image_fetch_size_limit():
    """외부 이미지는 상한까지만 받는다 — Content-Length가 크면 본문을 안 받고, 없거나 거짓이면 받다가 끊는다."""
    pulled = []

    class _Resp:
        def __init__(self, length, chunks):
            self.headers = {} if length is None else {"Content-Length": str(length)}
            self._chunks = chunks

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            for c in self._chunks:
                pulled.append(len(c))
                yield c

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    cases = {
        "https://x/declared-big": _Resp(11, [b"x" * 11]),
        "https://x/undeclared-big": _Resp(None, [b"x" * 6] * 100),  # 끝없이 오는 본문
        "https://x/ok": _Resp(None, [b"ab", b"cd"]),
    }
    originals = (video_service.requests.get, video_service.gcs.object_path_from_url,
                 video_service.MAX_RENDER_PHOTO_BYTES)
    video_service.requests.get = lambda url, **kw: cases[url]
    video_service.gcs.object_path_from_url = lambda url: None
    video_service.MAX_RENDER_PHOTO_BYTES = 10
    try:
        assert video_service._fetch_travel_image("https://x/declared-big") is None
        assert not pulled, "Content-Length가 상한을 넘는데 본문을 받았다"
        assert video_service._fetch_travel_image("https://x/undeclared-big") is None
        assert sum(pulled) <= 12, f"상한을 넘긴 뒤에도 계속 받았다: {sum(pulled)}바이트"
        assert video_service._fetch_travel_image("https://x/ok") == b"abcd"
    finally:
        (video_service.requests.get, video_service.gcs.object_path_from_url,
         video_service.MAX_RENDER_PHOTO_BYTES) = originals


if __name__ == "__main__":
    main()
