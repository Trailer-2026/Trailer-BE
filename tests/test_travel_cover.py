"""여행 대표 사진 자체 점검 — `python tests/test_travel_cover.py`.

GCS는 스텁으로 갈음한다(네트워크·버킷 없이 도는 게 목적).
프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.

핵심 규칙 셋을 지킨다: 지정 사진 > 첫 일정 이미지 폴백, 해제하면 폴백으로 복귀,
교체·해제 때 옛 객체를 버킷에서 지운다.
"""
import io
import sys
from datetime import date, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.exceptions.custom import BadRequestException, NotFoundException
from databases.daos import schedule_dao, travel_dao
from databases.models.base import Base
from databases.models.schedule import Schedule
from databases.models.travel import Travel
from databases.models.travel_image import TravelImage
from databases.models.travel_like import TravelLike
from databases.models.user import User
from services import travel_service
from utils import gcs

_BUCKET = "https://storage.googleapis.com/bucket/"


def _image(fmt: str = "JPEG", size: tuple[int, int] = (1, 1)) -> bytes:
    """검증을 통과하는 실물 이미지. 업로드 경로가 헤더를 파싱하므로 가짜 바이트는 못 쓴다."""
    buf = io.BytesIO()
    Image.new("RGB", size, (1, 2, 3)).save(buf, format=fmt)
    return buf.getvalue()


def _session():
    engine = create_engine("sqlite:///:memory:")
    # travel_image 는 이 테스트가 직접 쓰지 않지만 travel_detail 이 사진을 함께 읽으므로 필요하다.
    Base.metadata.create_all(
        engine,
        tables=[t.__table__ for t in (User, Travel, Schedule, TravelLike, TravelImage)],
    )
    db = sessionmaker(bind=engine)()
    for idx, nick in ((1, "owner"), (2, "other")):
        db.add(User(user_idx=idx, nickname=nick, provider="google", provider_id=f"p{idx}"))
    db.commit()
    return db


def _stub_gcs(monkey: dict):
    """버킷 대신 리스트에 기록한다 — (올린 경로들, 저장 MIME들, 지운 경로들)."""
    uploaded, mimes, deleted = [], [], []
    monkey["upload_bytes"] = gcs.upload_bytes
    monkey["object_path_from_url"] = gcs.object_path_from_url
    monkey["delete_object"] = gcs.delete_object

    gcs.upload_bytes = lambda path, data, ctype: (
        uploaded.append(path), mimes.append(ctype), _BUCKET + path,
    )[-1]
    gcs.object_path_from_url = lambda url: (
        url[len(_BUCKET):] if url.startswith(_BUCKET) else None
    )
    gcs.delete_object = lambda path: deleted.append(path)
    return uploaded, mimes, deleted


def _restore(monkey: dict):
    gcs.upload_bytes = monkey["upload_bytes"]
    gcs.object_path_from_url = monkey["object_path_from_url"]
    gcs.delete_object = monkey["delete_object"]


def _expect(exc, fn, what: str):
    try:
        fn()
    except exc:
        return
    raise AssertionError(f"{what}: {exc.__name__} 이 나야 한다")


def main() -> None:
    monkey: dict = {}
    uploaded, mimes, deleted = _stub_gcs(monkey)
    db = _session()
    owner = db.get(User, 1)
    other = db.get(User, 2)
    today = date.today()
    try:
        # 직접 만든 여행(일정 없음) → 일정 이미지가 없으니 지역 기본 사진이 뜬다(카드가 비지 않는다).
        manual = travel_dao.create(
            db, user_idx=1, title="직접 만든 여행", region="부산",
            start_date=today + timedelta(days=3), end_date=today + timedelta(days=4),
        )
        db.commit()
        card = next(c for c in travel_service.all_travels(db, owner).travels
                    if c.travel_idx == manual.travel_idx)
        assert card.cover_image_url == travel_service._DEFAULT_COVER_BY_REGION["부산"], card
        # 지역이 '부산역'(추천 저장 형식)이어도, 목록에 없는 지역이어도 값은 항상 있다.
        assert travel_service._default_cover("부산역") == travel_service._DEFAULT_COVER_BY_REGION["부산"]
        assert travel_service._default_cover("무릉도원") == travel_service._DEFAULT_COVER
        assert travel_service._default_cover(None) == travel_service._DEFAULT_COVER

        # 대표 사진 지정 → 카드·상세·홈 카드 모두 그 URL이 내려간다.
        result = travel_service.set_cover_image(db, owner, manual.travel_idx, _image(), "image/jpeg", "a.JPG")
        assert uploaded == [f"travel/{manual.travel_idx}/" + uploaded[0].split("/")[-1]], uploaded
        assert uploaded[0].endswith(".jpg"), uploaded  # 확장자를 살린다
        assert mimes[0] == "image/jpeg", mimes
        assert result.cover_image_url == _BUCKET + uploaded[0], result
        card = next(c for c in travel_service.all_travels(db, owner).travels
                    if c.travel_idx == manual.travel_idx)
        assert card.cover_image_url == result.cover_image_url, card
        detail = travel_service.travel_detail(db, owner, manual.travel_idx)
        assert detail.cover_image_url == result.cover_image_url, detail
        assert travel_service.current_travel(db, owner).cover_image_url == result.cover_image_url

        # 교체 → 새 URL로 바뀌고 옛 객체는 버킷에서 지워진다.
        first_path = uploaded[0]
        replaced = travel_service.set_cover_image(db, owner, manual.travel_idx, _image("PNG"), "image/png", "b.png")
        assert replaced.cover_image_url != result.cover_image_url, replaced
        assert deleted == [first_path], deleted

        # 파일명이 수상하면 확장자를 버린다 — 객체 경로 모양이 파일명에 휘둘리면 안 된다.
        travel_service.set_cover_image(db, owner, manual.travel_idx, _image(), "image/jpeg", "a.jpg/../x")
        assert uploaded[-1].count("/") == 2, uploaded[-1]  # travel/{idx}/{uuid} 3토막 고정

        # 뒷정리(옛 객체 삭제)가 터져도 요청 자체는 성공해야 한다 — DB엔 이미 반영됐다.
        alive, gcs.delete_object = gcs.delete_object, lambda path: 1 / 0
        assert travel_service.set_cover_image(
            db, owner, manual.travel_idx, _image(), "image/jpeg", "d.jpg"
        ).cover_image_url.startswith(_BUCKET)
        assert travel_service.delete_cover_image(db, owner, manual.travel_idx) is not None
        gcs.delete_object = alive

        # AI 추천 여행: 첫 일정 이미지가 폴백. 지정 사진이 있으면 그게 이긴다.
        ai = travel_dao.create(
            db, user_idx=1, title="부산 여행",
            start_date=today - timedelta(days=10), end_date=today - timedelta(days=9),
        )
        for seq, image in ((0, None), (1, "http://tong.visitkorea.or.kr/a.jpg")):
            schedule_dao.create(
                db, travel_idx=ai.travel_idx, user_idx=1, day_no=1, sequence=seq,
                kind="visit", title=f"장소{seq}", start_time=time(10, 0), end_time=time(11, 0),
                latitude=35.0, longitude=129.0, image_url=image,
            )
        db.commit()
        past = travel_service.past_travels(db, owner).travels[0]
        assert past.cover_image_url == "http://tong.visitkorea.or.kr/a.jpg", past
        travel_service.set_cover_image(db, owner, ai.travel_idx, _image(), "image/jpeg", "c.jpg")
        past = travel_service.past_travels(db, owner).travels[0]
        assert past.cover_image_url.startswith(_BUCKET), past

        # 해제 → 첫 일정 이미지 폴백으로 복귀하고 올렸던 객체도 지워진다.
        before = len(deleted)
        cleared = travel_service.delete_cover_image(db, owner, ai.travel_idx)
        assert cleared.cover_image_url == "http://tong.visitkorea.or.kr/a.jpg", cleared
        assert len(deleted) == before + 1, deleted
        # 일정 이미지가 없는 여행은 해제 후 지역 기본 사진으로. 이미 없어도 성공(멱등), 지울 객체도 없다.
        default_busan = travel_service._DEFAULT_COVER_BY_REGION["부산"]
        cleared = travel_service.delete_cover_image(db, owner, manual.travel_idx)
        assert cleared.cover_image_url == default_busan, cleared
        before = len(deleted)
        assert travel_service.delete_cover_image(db, owner, manual.travel_idx).cover_image_url == default_busan
        assert len(deleted) == before, "지정된 사진이 없으면 삭제 호출도 없어야 한다"

        # 검증 실패는 버킷에 올리기 전에 끊긴다.
        before = len(uploaded)
        _expect(BadRequestException, lambda: travel_service.set_cover_image(
            db, owner, manual.travel_idx, b"x", "application/pdf", "a.pdf"), "이미지 아님")
        _expect(BadRequestException, lambda: travel_service.set_cover_image(
            db, owner, manual.travel_idx, b"", "image/jpeg", "a.jpg"), "빈 파일")
        _expect(BadRequestException, lambda: travel_service.set_cover_image(
            db, owner, manual.travel_idx, b"\0" * (gcs.MAX_IMAGE_BYTES + 1), "image/jpeg", "a.jpg"),
            "크기 초과")
        # content_type만 image/*로 적어 보낸 비이미지는 바이트 판별에서 걸린다.
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        _expect(BadRequestException, lambda: travel_service.set_cover_image(
            db, owner, manual.travel_idx, svg, "image/svg+xml", "x.svg"), "SVG")
        _expect(BadRequestException, lambda: travel_service.set_cover_image(
            db, owner, manual.travel_idx, b"<html><script>alert(1)</script>", "image/png", "x.png"), "HTML 위조")
        _expect(BadRequestException, lambda: travel_service.set_cover_image(
            db, owner, manual.travel_idx, _image()[:20], "image/jpeg", "x.jpg"), "헤더가 깨진 JPEG")
        assert len(uploaded) == before, "검증 실패분이 버킷에 올라갔다"

        # 반대로 '헤더는 온전하고 뒤만 잘린 JPEG'는 통과한다 — 의도된 한계(gcs._detect_image_mime 참고).
        big = _image("JPEG", (200, 200))
        travel_service.set_cover_image(
            db, owner, manual.travel_idx, big[: int(len(big) * 0.7)], "image/jpeg", "cut.jpg",
        )
        assert mimes[-1] == "image/jpeg", mimes[-1]

        # 저장 MIME은 클라이언트 말이 아니라 실물에서 정한다 — PNG를 image/jpeg 라 우겨도 image/png.
        travel_service.set_cover_image(db, owner, manual.travel_idx, _image("PNG"), "image/jpeg", "liar.jpg")
        assert mimes[-1] == "image/png", mimes[-1]
        # HEIC(아이폰 원본)는 디코더가 없어도 매직바이트로 통과시킨다.
        heic = b"\0\0\0 ftypheic" + b"\0" * 64
        travel_service.set_cover_image(db, owner, manual.travel_idx, heic, "image/heic", "p.heic")
        assert mimes[-1] == "image/heic", mimes[-1]

        # 남의 여행·없는 여행은 404 (403 아님).
        _expect(NotFoundException, lambda: travel_service.set_cover_image(
            db, other, manual.travel_idx, _image(), "image/jpeg", "a.jpg"), "남의 여행 지정")
        _expect(NotFoundException, lambda: travel_service.delete_cover_image(db, other, manual.travel_idx),
                "남의 여행 해제")
        _expect(NotFoundException, lambda: travel_service.set_cover_image(
            db, owner, 9999, _image(), "image/jpeg", "a.jpg"), "없는 여행")

        print("OK: 여행 대표 사진 자체 점검 통과")
    finally:
        db.close()
        _restore(monkey)


if __name__ == "__main__":
    main()
