"""여행 저장 서비스 — 추천 플랜(캐시)을 Travel + schedule로 영속화한다.

프론트는 plan_id만 보내고(긴 코스 재전송 X), 서버가 plan_cache에서 그 플랜(Itinerary)을 꺼내
세그먼트를 그대로 스케줄로 저장한다. 좌표는 방문지·숙소는 자기 좌표, 기차는 출발역 좌표를
station 테이블에서 채운다. 추천 관광지/숙소 대표 이미지는 schedule.image_url에 저장한다.
숙소는 시각이 없어 'ⓐ 그날 마지막 방문 종료~다음날 첫 일정 시작'으로 잡는다.
"""
import logging
from datetime import date, datetime, time, timedelta
from typing import BinaryIO

from sqlalchemy.orm import Session

from core.enums import TravelSource
from core.exceptions.custom import BadRequestException, NotFoundException
from databases.daos import (
    schedule_dao, station_dao, travel_dao, travel_image_dao, travel_like_dao, user_dao,
)
from recommend.routing import haversine  # 거리 계산 단일 기준(km) — 사진→일정 자동 매핑에 쓴다
from services import push_service
from schemas.travel_schema import (
    HomeTravelCard,
    PastTravelCard,
    PastTravelListResponse,
    ScheduleCreateRequest,
    ScheduleUpdateRequest,
    TrainTicketResponse,
    TravelCoverImageResponse,
    TravelDayGroup,
    TravelDetailResponse,
    TravelImageItem,
    TravelImagesResponse,
    TravelCard,
    TravelListResponse,
    TravelManualCreateRequest,
    TravelResponse,
    TravelScheduleItem,
    TravelTicketsResponse,
)
from utils import gcs, plan_cache
from utils.timezone import now_kst

logger = logging.getLogger(__name__)

_LODGING_CHECKIN = time(21, 0)   # 마지막 방문 종료를 못 구할 때 체크인 기본값
_LODGING_CHECKOUT = time(9, 0)   # 다음날 첫 일정 시작을 못 구할 때 체크아웃 기본값
_DEFAULT_TIME = time(9, 0)       # 방문/기차 시각이 비어 있을 때 안전 기본값

# 지역별 기본 커버 사진 — 지정 사진도, 일정 이미지도 없을 때 카드가 비지 않게 쓴다.
# 직접 만든 여행이 그렇다: 장소 검색이 카카오 로컬이라 이미지가 없어 schedule.image_url이 늘 빈다.
# 값은 AI 추천 커버와 같은 출처(한국관광공사 TourAPI firstimage)라 새로 관리할 게 없다 —
# 링크가 죽으면 이 상수만 갈아끼운다. 키는 travel.region('역' 떼기 전 도착역명 기준).
_DEFAULT_COVER_BY_REGION = {
    "서울": "https://tong.visitkorea.or.kr/cms/resource/98/3487598_image2_1.jpg",   # 경복궁
    "부산": "https://tong.visitkorea.or.kr/cms/resource/34/3090534_image2_1.JPG",   # 해운대해수욕장
    "대구": "https://tong.visitkorea.or.kr/cms/resource/56/3572056_image2_1.jpg",   # 팔공산 케이블카
    "인천": "https://tong.visitkorea.or.kr/cms/resource/94/3518594_image2_1.jpg",   # 월미도
    "광주": "https://tong.visitkorea.or.kr/cms/resource/66/3495266_image2_1.jpg",   # 무등산국립공원
    "대전": "https://tong.visitkorea.or.kr/cms/resource_photo/22/3514122_image2_1.jpg",  # 한밭수목원
    "울산": "https://tong.visitkorea.or.kr/cms/resource/21/3008621_image2_1.jpg",   # 태화강
    "강릉": "https://tong.visitkorea.or.kr/cms/resource/52/3501452_image2_1.jpg",   # 강릉 경포대
    "동해": "https://tong.visitkorea.or.kr/cms/resource/83/2921483_image2_1.jpg",   # 추암 촛대바위
    "춘천": "https://tong.visitkorea.or.kr/cms/resource/45/4067545_image2_1.jpg",   # 남이섬
    "원주": "https://tong.visitkorea.or.kr/cms/resource/35/3332635_image2_1.jpg",   # 소금산 출렁다리
    "여수": "https://tong.visitkorea.or.kr/cms/resource/34/3019934_image2_1.jpg",   # 오동도 등대
    "순천": "https://tong.visitkorea.or.kr/cms/resource/14/4088614_image2_1.jpg",   # 순천만습지
    "목포": "https://tong.visitkorea.or.kr/cms/resource/11/4067011_image2_1.JPG",   # 유달산
    "전주": "https://tong.visitkorea.or.kr/cms/resource_photo/45/3365745_image2_1.jpg",  # 전주 경기전
    "경주": "https://tong.visitkorea.or.kr/cms/resource/70/3506170_image2_1.jpg",   # 경주 불국사
    "포항": "https://tong.visitkorea.or.kr/cms/resource/98/3410998_image2_1.jpg",   # 호미곶 등대
    "안동": "https://tong.visitkorea.or.kr/cms/resource/62/4059762_image2_1.jpg",   # 안동 하회마을
    "진주": "https://tong.visitkorea.or.kr/cms/resource/65/4062165_image2_1.JPG",   # 진주성
    "천안": "https://tong.visitkorea.or.kr/cms/resource/21/3450021_image2_1.jpg",   # 독립기념관
    "제주": "https://tong.visitkorea.or.kr/cms/resource/43/4094043_image2_1.jpg",   # 천지연폭포
}
# 목록에 없는 지역·지역 미입력용. 기차 여행이라 역 사진으로 둔다(정동진역).
_DEFAULT_COVER = "https://tong.visitkorea.or.kr/cms/resource/12/4093712_image2_1.jpg"
_TITLE_MAX = 100                 # travel.title 컬럼 길이 — 자동 생성 제목이 넘으면 INSERT가 깨진다
# 여행 사진 한 요청당 장수 상한. 라우터가 이 값+1장까지만 읽어 초과 요청의 바이트를
# 통째로 메모리에 올리지 않는다(장당 10MB × 무제한을 막는 게 목적).
MAX_TRAVEL_IMAGES = 20
# 편집 시 null로 지우면 안 되는 NOT NULL 컬럼(schedule) — 명시적 null 요청을 400으로 막는다.
_NON_NULL_EDIT_FIELDS = ("title", "start_time", "end_time", "latitude", "longitude")


def save_selected_plan(db: Session, user, plan_id: str) -> TravelResponse:
    """선택한 추천 플랜(plan_id)을 사용자의 여행으로 저장한다. 서비스가 트랜잭션을 소유(commit)."""
    _ensure_no_active_travel(db, user)
    payload = plan_cache.get(plan_id)
    if payload is None:
        raise BadRequestException("추천이 만료되었습니다. 다시 추천받아 주세요.")

    it = payload["itinerary"]
    start_date = datetime.strptime(payload["go_date"], "%Y%m%d").date()
    end_date = datetime.strptime(payload["back_date"], "%Y%m%d").date()
    fallback = payload.get("fallback_coord")  # (lat, lng) — 기차역 좌표 조회 실패 시 대체

    travel = travel_dao.create(
        db, user_idx=user.user_idx,
        title=_travel_title(payload.get("region"), start_date, end_date, it.title),
        start_date=start_date, end_date=end_date, region=payload.get("region"),
        status="PLANNED", source=TravelSource.RECOMMEND,
    )

    day_first, day_last = _day_bounds(it.segments)
    seq: dict[int, int] = {}
    count = 0
    for seg in it.segments:
        fields = _schedule_fields(db, seg, day_first, day_last, fallback)
        if fields is None:
            continue
        seq[seg.day_no] = seq.get(seg.day_no, 0)
        schedule_dao.create(
            db, travel_idx=travel.travel_idx, user_idx=user.user_idx,
            day_no=seg.day_no, sequence=seq[seg.day_no], **fields,
        )
        seq[seg.day_no] += 1
        count += 1

    db.commit()
    # '여행을 담았습니다' 푸시 — 커밋 뒤에 보낸다. 알림이 실패해도 저장은 되돌리지 않는다.
    push_service.notify_travel_saved(db, user.user_idx, travel)
    return TravelResponse(
        travel_idx=travel.travel_idx, title=travel.title,
        start_date=travel.start_date, end_date=travel.end_date,
        region=travel.region, status=travel.status, schedule_count=count,
    )


def create_manual(db: Session, user, req: TravelManualCreateRequest) -> TravelResponse:
    """직접 일정 만들기 — 빈 여행 1건 생성. 일정 항목은 add_schedule로 이후 추가한다."""
    if req.end_date < req.start_date:
        raise BadRequestException("종료일이 시작일보다 빠를 수 없습니다.")
    _ensure_no_active_travel(db, user)

    # 제목 미입력·공백이면 AI 저장과 동일하게 지역·기간으로 자동 생성('부산 3박 4일 여행' 형태).
    title = (req.title or "").strip() or _travel_title(
        req.region, req.start_date, req.end_date, None
    )
    travel = travel_dao.create(
        db, user_idx=user.user_idx, title=title,
        start_date=req.start_date, end_date=req.end_date,
        region=req.region, status="PLANNED", source=TravelSource.MANUAL,
    )
    db.commit()
    push_service.notify_travel_saved(db, user.user_idx, travel)  # 커밋 뒤 발송(실패해도 무해)
    return TravelResponse(
        travel_idx=travel.travel_idx, title=travel.title,
        start_date=travel.start_date, end_date=travel.end_date,
        region=travel.region, status=travel.status, schedule_count=0,
    )


def add_schedule(db: Session, user, travel_idx: int, req: ScheduleCreateRequest) -> TravelScheduleItem:
    """여행에 일정 항목(장소/기차) 1건 추가. 본인 여행이 아니면 404. sequence는 그날 끝에 append.

    travel 행을 FOR UPDATE로 잠근 뒤 next_sequence(max+1)→create를 수행해, 같은 여행에 동시
    추가가 와도 sequence가 중복되지 않도록 직렬화한다(커밋 시 락 해제).
    """
    travel = travel_dao.get_for_update(db, travel_idx)
    if travel is None or travel.user_idx != user.user_idx:
        raise NotFoundException("여행을 찾을 수 없습니다.")

    total_days = (travel.end_date - travel.start_date).days + 1
    day_no, fields = _manual_schedule_fields(db, req, travel, total_days)
    sequence = schedule_dao.next_sequence(db, travel_idx, day_no)
    schedule = schedule_dao.create(
        db, travel_idx=travel_idx, user_idx=user.user_idx,
        day_no=day_no, sequence=sequence, memo=req.memo, **fields,
    )
    db.commit()
    return TravelScheduleItem.model_validate(schedule)


def update_schedule(
    db: Session, user, travel_idx: int, schedule_idx: int, req: ScheduleUpdateRequest,
) -> TravelScheduleItem:
    """일정 항목 편집 — 보낸 필드만 반영. 본인 여행의 항목이 아니면 404."""
    schedule = _owned_schedule(db, user, travel_idx, schedule_idx)
    data = req.model_dump(exclude_unset=True)
    # NOT NULL 컬럼을 명시적 null로 지우려 하면 커밋 시 500 대신 400으로 막는다.
    if any(f in data and data[f] is None for f in _NON_NULL_EDIT_FIELDS):
        raise BadRequestException("제목·시각·좌표는 빈 값(null)으로 지울 수 없습니다.")
    # 병합 후 최종 시각으로 순서 검증 — 보낸 값이 없으면 기존 값을 쓴다.
    _validate_time_order(data.get("start_time", schedule.start_time), data.get("end_time", schedule.end_time))
    for field, value in data.items():
        setattr(schedule, field, value)
    db.commit()
    return TravelScheduleItem.model_validate(schedule)


def delete_schedule(db: Session, user, travel_idx: int, schedule_idx: int) -> None:
    """일정 항목 소프트 삭제. 본인 여행의 항목이 아니면 404.

    거기 붙어 있던 사진은 지우지 않고 여행 소속으로 떼어 둔다 — 사진의 소유는 여행이고,
    일정을 지웠다고 찍은 사진까지 사라지면 안 된다(영상에선 마지막 지점 뒤로 간다).
    """
    schedule = _owned_schedule(db, user, travel_idx, schedule_idx)
    travel_image_dao.detach_schedule(db, schedule_idx)
    schedule_dao.soft_delete(db, schedule)
    db.commit()


def rename_travel(db: Session, user, travel_idx: int, title: str) -> TravelResponse:
    """여행 제목 변경. 본인 여행이 아니면 404.

    빈 값·공백이면 생성(create_manual) 때와 같이 지역·기간으로 자동 생성한다 — '이름 지우기'를
    빈 제목으로 두는 대신 기본 제목으로 되돌리는 쪽이 화면에 빈 칸이 남지 않는다.
    """
    travel = _owned_travel(db, user, travel_idx)
    new_title = (title or "").strip() or _travel_title(
        travel.region, travel.start_date, travel.end_date, None
    )
    travel_dao.update_title(db, travel, new_title)
    db.commit()
    return TravelResponse(
        travel_idx=travel.travel_idx, title=travel.title,
        start_date=travel.start_date, end_date=travel.end_date, region=travel.region,
        # status 컬럼은 항상 PLANNED라 조회와 같은 규칙(기간·오늘 KST)으로 계산해 돌려준다.
        status=_effective_status(travel.start_date, travel.end_date, now_kst().date()),
        schedule_count=schedule_dao.count_by_travel(db, travel_idx),
    )


def set_cover_image(
    db: Session, user, travel_idx: int, data: bytes, content_type: str | None, filename: str | None,
) -> TravelCoverImageResponse:
    """여행 대표 사진 지정·교체 — 업로드 이미지를 GCS에 올려 travel.cover_image_url에 박는다.

    직접 만든 여행은 일정에 이미지가 없어 폴백(첫 일정 image_url)이 비므로 이 지정이 유일한
    썸네일이다. AI 추천으로 저장한 여행에도 같은 엔드포인트로 덮어쓸 수 있다.
    """
    travel = _owned_travel(db, user, travel_idx)
    previous = travel.cover_image_url
    url = gcs.upload_image(f"travel/{travel_idx}", data, content_type, filename)
    travel_dao.update_cover_image(db, travel, url)
    db.commit()
    _drop_uploaded_image(previous)  # 교체 성공(커밋) 뒤에 옛 사진을 지운다 — 실패 시엔 남겨야 한다
    return TravelCoverImageResponse(travel_idx=travel.travel_idx, cover_image_url=url)


def delete_cover_image(db: Session, user, travel_idx: int) -> TravelCoverImageResponse:
    """대표 사진 지정 해제 — 지우면 첫 일정 이미지, 그것도 없으면 지역 기본 사진으로 돌아간다."""
    travel = _owned_travel(db, user, travel_idx)
    previous = travel.cover_image_url
    travel_dao.update_cover_image(db, travel, None)
    db.commit()
    _drop_uploaded_image(previous)
    return TravelCoverImageResponse(
        travel_idx=travel.travel_idx,
        cover_image_url=schedule_dao.cover_image(db, travel_idx) or _default_cover(travel.region),
    )


def add_images(
    db: Session, user, travel_idx: int, schedule_idx: int | None,
    files: list[tuple[BinaryIO, str | None, str | None]],
) -> TravelImagesResponse:
    """여행 중 찍은 사진들을 여행에 붙인다 — GCS 업로드 후 travel_image 행으로 남긴다.

    사진의 소유는 여행이고 일정(schedule_idx)은 선택이다. **안 주면 서버가 사진 EXIF의
    GPS로 가장 가까운 일정을 찾아 자동 매핑한다** — 앱은 '여행 + 사진'만 보내면 되고,
    영상에서 사진이 찍힌 자리에 뜬다. 좌표를 못 구한 사진은 일정 없이(NULL) 저장돼
    영상 마지막 지점에 몰려 나온다. 그래서 일정이 하나도 없는 여행에도 사진은 올라간다.

    본문(bytes)이 아니라 **파일 스트림**을 받는다 — 20장을 한꺼번에 메모리에 올리지 않으려고
    아래 루프에서 한 장씩 읽고 버린다. 장수 검증도 읽기 전에 끝낸다.
    """
    if len(files) > MAX_TRAVEL_IMAGES:
        raise BadRequestException(f"사진은 한 번에 {MAX_TRAVEL_IMAGES}장까지 올릴 수 있습니다.")

    _owned_travel(db, user, travel_idx)
    if schedule_idx is not None:
        _owned_schedule(db, user, travel_idx, schedule_idx)  # 그 여행 소속인지 검증(아니면 404)
    # 자동 매핑용 후보. 사용자가 일정을 직접 골랐으면 좌표를 볼 필요가 없어 조회도 안 한다.
    candidates = [] if schedule_idx is not None else schedule_dao.list_by_travel(db, travel_idx)

    images = []
    for stream, content_type, filename in files:
        # 대표 사진과 같은 이유로 상한+1바이트까지만 읽는다(초과분을 메모리에 안 올린다).
        # 다음 회차에서 data가 새로 묶이며 직전 장은 바로 풀린다 — 동시에 드는 건 1장뿐.
        data = stream.read(gcs.MAX_IMAGE_BYTES + 1)
        # 업로드(형식·크기 검증 포함)를 먼저 통과시킨 뒤에 좌표를 본다 — 어차피 거절될
        # 파일의 EXIF를 파싱하지 않으려는 것.
        url = gcs.upload_image(f"travel/{travel_idx}/photos", data, content_type, filename)
        target = schedule_idx if schedule_idx is not None else _snap_to_schedule(data, candidates)
        images.append(travel_image_dao.create(db, travel_idx, target, url))
    db.commit()
    return TravelImagesResponse(
        travel_idx=travel_idx,
        images=[TravelImageItem.model_validate(image) for image in images],
    )


def _snap_to_schedule(data: bytes, schedules) -> int | None:
    """사진 EXIF의 GPS 좌표에서 가장 가까운 일정의 schedule_idx. 못 정하면 None.

    앱은 '여행 + 사진'만 보내므로 어느 일정에 붙일지는 서버가 정한다. 사진이 찍힌 위치와
    일정 좌표(방문지는 자기 좌표, 기차는 출발역)를 직선거리로 재 제일 가까운 것을 고른다.

    거리 상한은 두지 않는다 — 아무리 멀어도 '가장 가까운 일정'이 대안(None → 영상 마지막
    지점에 몰아 붙이기)보다 나쁠 수 없어서다.

    None이 되는 경우: 일정이 없는 여행, GPS 없는 사진(메신저 전송본은 GPS가 지워진다),
    깨진 EXIF. 저장은 되고 영상에서 마지막 지점 뒤에 나온다.
    """
    if not schedules:
        return None
    # 렌더러(무거운 의존)를 모듈 로드 시점에 끌어오지 않으려고 여기서 import 한다.
    # photos-only 렌더가 쓰는 파서를 그대로 재사용 — EXIF 해석 규칙을 두 벌로 만들지 않는다.
    from services.video_service import _extract_photo_meta

    meta = _extract_photo_meta(data)
    if meta is None:
        return None
    lat, lng = float(meta["latitude"]), float(meta["longitude"])
    nearest = min(schedules, key=lambda s: haversine(lat, lng, s.latitude, s.longitude))
    return nearest.schedule_idx


def delete_image(db: Session, user, travel_idx: int, image_idx: int) -> None:
    """붙인 사진 1장 삭제 — 행은 소프트 삭제하고 저장소 객체는 지운다. 남의 사진·다른 여행이면 404.

    행에 deleted_at만 찍히므로 조회에서는 사라지지만, GCS 객체는 실제로 지워 보관 비용을
    남기지 않는다 — 되살리기용이 아니라 감사 흔적이다.
    """
    _owned_travel(db, user, travel_idx)
    image = travel_image_dao.get_by_idx(db, image_idx)
    if image is None or image.travel_idx != travel_idx:
        raise NotFoundException("사진을 찾을 수 없습니다.")

    url = image.url
    travel_image_dao.delete(db, image)
    db.commit()
    _drop_uploaded_image(url)  # 커밋 뒤 뒷정리 — 실패해도 삼킨다


def _default_cover(region: str | None) -> str:
    """지역별 기본 커버 사진. region은 '부산역'(추천 저장)·'부산'(직접 입력) 두 형태라 접미사를 뗀다."""
    return _DEFAULT_COVER_BY_REGION.get((region or "").strip().removesuffix("역"), _DEFAULT_COVER)


def _drop_uploaded_image(url: str | None) -> None:
    """우리 버킷에 올린 이미지면 객체까지 지운다(외부 URL·None은 무시).

    커밋 뒤에 도는 뒷정리라 무슨 일이 있어도 예외를 올리지 않는다 — 여기서 502가 나면
    (버킷 설정 문제 등) DB에는 이미 반영된 요청이 실패한 것처럼 보인다. 고아 객체가 남는 게 낫다.
    """
    if not url:
        return
    try:
        path = gcs.object_path_from_url(url)
        if path:
            gcs.delete_object(path)
    except Exception:  # noqa: BLE001  뒷정리 실패는 삼킨다
        logger.warning("업로드 이미지 객체 정리 실패(무시): %s", url)


def delete_travel(db: Session, user, travel_idx: int) -> None:
    """여행 소프트 삭제 — 그 여행의 일정 항목도 함께 삭제한다. 본인 여행이 아니면 404.

    삭제하면 get_active_travel에서도 빠지므로 '예정 여행 1개' 제약이 풀려 새 여행을 만들 수 있다.
    좋아요(travel_like)는 FK 하드 행이라 남기지만, 목록·상세가 모두 여행 기준이라 노출되지 않는다.
    """
    travel = _owned_travel(db, user, travel_idx)
    schedule_dao.soft_delete_by_travel(db, travel_idx)
    travel_dao.soft_delete(db, travel)
    db.commit()
    push_service.notify_travel_deleted(db, user.user_idx, travel)  # 커밋 뒤 발송(실패해도 무해)


def current_travel(db: Session, user) -> HomeTravelCard | None:
    """홈 화면 여행 카드 1건 — 진행 중 우선, 없으면 가장 가까운 예정, 둘 다 없으면 None.

    status 컬럼은 저장 시 항상 PLANNED이고 자동 전환하는 배치가 없으므로, 진행 여부는
    KST 오늘과 여행 기간으로 계산한다(스케줄러 불필요).
    """
    travels = travel_dao.list_by_user(db, user.user_idx)
    today = now_kst().date()
    ongoing = [t for t in travels if t.start_date <= today <= t.end_date]
    upcoming = sorted((t for t in travels if t.start_date > today), key=lambda t: t.start_date)
    chosen = ongoing[0] if ongoing else (upcoming[0] if upcoming else None)
    if chosen is None:
        return None
    return HomeTravelCard(
        travel_idx=chosen.travel_idx, title=chosen.title,
        start_date=chosen.start_date, end_date=chosen.end_date,
        status=_effective_status(chosen.start_date, chosen.end_date, today),
        # 지정 사진 → 첫 일정 이미지(AI 추천 여행) → 지역 기본 사진 순. 카드 사진은 절대 비지 않는다.
        cover_image_url=chosen.cover_image_url
        or schedule_dao.cover_image(db, chosen.travel_idx)
        or _default_cover(chosen.region),
    )


def all_travels(db: Session, user) -> TravelListResponse:
    """전체 여행 목록 — 예정·진행 중·지난 여행을 한 번에. 각 카드의 status는 날짜로 계산한다.

    정렬은 화면 순서 그대로다: 아직 안 끝난 여행(진행 중·예정)을 시작일 오름차순으로 위에 두고
    (진행 중이 start_date가 가장 이르므로 자연히 맨 위), 그 뒤에 지난 여행을 최근 종료순으로 붙인다.
    썸네일·좋아요는 past_travels와 같이 각 1쿼리로 일괄 조회한다(N+1 회피).
    """
    today = now_kst().date()
    travels = travel_dao.list_by_user(db, user.user_idx)
    idxs = [t.travel_idx for t in travels]
    covers = schedule_dao.cover_images(db, idxs)
    liked = travel_like_dao.liked_travel_idxs(db, user.user_idx, idxs)

    upcoming = sorted((t for t in travels if t.end_date >= today), key=lambda t: t.start_date)
    past = sorted((t for t in travels if t.end_date < today), key=lambda t: t.end_date, reverse=True)

    cards = [
        TravelCard(
            travel_idx=t.travel_idx, title=t.title,
            start_date=t.start_date, end_date=t.end_date,
            status=_effective_status(t.start_date, t.end_date, today),
            cover_image_url=t.cover_image_url or covers.get(t.travel_idx) or _default_cover(t.region),
            liked=t.travel_idx in liked,
        )
        for t in upcoming + past
    ]
    return TravelListResponse(travels=cards, total=len(cards))


def past_travels(db: Session, user) -> PastTravelListResponse:
    """여행기록 화면의 '지난 여행' 목록 — 이미 종료된 여행만 최신순으로.

    종료 여부는 _effective_status의 COMPLETED 정의(오늘 > end_date)와 같이 날짜로 판정한다.
    썸네일·좋아요 여부는 여행 수와 무관하게 각 1쿼리로 일괄 조회한다(N+1 회피).
    """
    today = now_kst().date()
    travels = travel_dao.list_completed_by_user(db, user.user_idx, today)
    idxs = [t.travel_idx for t in travels]
    covers = schedule_dao.cover_images(db, idxs)
    liked = travel_like_dao.liked_travel_idxs(db, user.user_idx, idxs)

    cards = [
        PastTravelCard(
            travel_idx=t.travel_idx, title=t.title,
            start_date=t.start_date, end_date=t.end_date,
            status="COMPLETED",
            cover_image_url=t.cover_image_url or covers.get(t.travel_idx) or _default_cover(t.region),
            liked=t.travel_idx in liked,
        )
        for t in travels
    ]
    return PastTravelListResponse(travels=cards, total=len(cards))


def travel_detail(db: Session, user, travel_idx: int) -> TravelDetailResponse:
    """여행 1건의 일정표(일자별 타임라인) 상세. 본인 여행이 아니거나 없으면 404."""
    travel = travel_dao.get_by_idx(db, travel_idx)
    if travel is None or travel.user_idx != user.user_idx:
        raise NotFoundException("여행을 찾을 수 없습니다.")

    schedules = schedule_dao.list_by_travel(db, travel_idx)
    # 사진은 여행 단위로 한 번에 읽고 일정별로 묶는다(쿼리 1회). 일정에 안 붙은 사진은
    # 어느 일정에도 안 들어가고 응답 최상단 images로 따로 나간다.
    photos = travel_image_dao.by_travel(db, travel_idx)
    by_schedule: dict[int, list[TravelImageItem]] = {}
    for photo in photos:
        if photo.schedule_idx is not None:
            by_schedule.setdefault(photo.schedule_idx, []).append(
                TravelImageItem.model_validate(photo)
            )
    groups: dict[int, TravelDayGroup] = {}
    for s in schedules:  # list_by_travel이 (day_no, sequence) 순 정렬을 보장
        group = groups.get(s.day_no)
        if group is None:
            group = TravelDayGroup(
                day_no=s.day_no,
                date=travel.start_date + timedelta(days=s.day_no - 1),
                items=[],
            )
            groups[s.day_no] = group
        item = TravelScheduleItem.model_validate(s)
        item.images = by_schedule.get(s.schedule_idx, [])
        group.items.append(item)

    today = now_kst().date()
    return TravelDetailResponse(
        travel_idx=travel.travel_idx, title=travel.title,
        start_date=travel.start_date, end_date=travel.end_date, region=travel.region,
        status=_effective_status(travel.start_date, travel.end_date, today),
        # 일정 이미지 폴백은 이미 읽어 둔 schedules에서 고른다(정렬이 보장돼 별도 쿼리가 필요 없다).
        cover_image_url=travel.cover_image_url
        or next((s.image_url for s in schedules if s.image_url), None)
        or _default_cover(travel.region),
        days=[groups[k] for k in sorted(groups)],
        images=[
            TravelImageItem.model_validate(p) for p in photos if p.schedule_idx is None
        ],
    )


def travel_tickets(db: Session, user, travel_idx: int) -> TravelTicketsResponse:
    """여행 1건의 승차권 목록 — AI 추천 일정을 승인(저장)한 여행에서만 조회할 수 있다.

    travel 행은 추천 플랜 승인 시에만 생기므로, 본인 여행 존재 확인이 곧 승인 여부 검증이다.
    미승인(저장 안 됨)·타인 여행은 404. 승차권은 kind=train 일정에서 만들며, 좌석·호차·타는곳
    등 예매 정보는 보유하지 않아 응답에 포함하지 않는다.
    """
    travel = travel_dao.get_by_idx(db, travel_idx)
    if travel is None or travel.user_idx != user.user_idx:
        raise NotFoundException("승인된 여행 일정을 찾을 수 없습니다.")

    trains = schedule_dao.list_trains_by_travel(db, travel_idx)
    tickets = [
        TrainTicketResponse(
            schedule_idx=s.schedule_idx,
            day_no=s.day_no,
            date=travel.start_date + timedelta(days=s.day_no - 1),
            train_grade=s.train_grade or "",
            train_no=s.train_no or "",
            dep_station=s.dep_station or "",
            arr_station=s.arr_station or "",
            dep_time=s.start_time,
            arr_time=s.end_time,
            car_no=s.car_no,
            seat_no=s.seat_no,
        )
        for s in trains
    ]
    return TravelTicketsResponse(travel_idx=travel.travel_idx, tickets=tickets)


def _ensure_no_active_travel(db: Session, user) -> None:
    """'예정 여행은 1개만' — 종료 안 된 여행이 이미 있으면 400. 지난(종료) 여행은 세지 않는다.

    검사~생성이 한 트랜잭션 안에서 사용자별로 직렬화되도록 먼저 user 행을 잠근다 — 동시
    생성 요청이 둘 다 검사를 통과해 예정 여행이 2개 만들어지는 경합을 막는다(커밋 시 해제).
    """
    user_dao.lock_by_idx(db, user.user_idx)
    if travel_dao.get_active_travel(db, user.user_idx, now_kst().date()) is not None:
        raise BadRequestException("이미 예정된 여행이 있습니다. 기존 여행을 삭제한 뒤 새로 만들어 주세요.")


def _owned_travel(db: Session, user, travel_idx: int):
    """수정·삭제 대상 여행을 본인 것으로 검증해 반환. 없거나 남의 여행이면 404(403 아님)."""
    travel = travel_dao.get_by_idx(db, travel_idx)
    if travel is None or travel.user_idx != user.user_idx:
        raise NotFoundException("여행을 찾을 수 없습니다.")
    return travel


def _owned_schedule(db: Session, user, travel_idx: int, schedule_idx: int):
    """편집·삭제 대상 일정 항목을 본인 여행 소속으로 검증해 반환. 아니면 404."""
    schedule = schedule_dao.get_by_idx(db, schedule_idx)
    if schedule is None or schedule.travel_idx != travel_idx or schedule.user_idx != user.user_idx:
        raise NotFoundException("일정 항목을 찾을 수 없습니다.")
    return schedule


def _validate_time_order(start_time, end_time) -> None:
    """종료(도착) 시각이 시작(출발) 시각보다 빠르면 400 — 자정 넘김은 다루지 않는다(같은 날 기준).

    둘 중 하나가 없으면 검증하지 않는다(편집에서 한쪽만 바뀔 때 상대는 기존 값이 넘어온다).
    """
    if start_time is not None and end_time is not None and end_time < start_time:
        raise BadRequestException("종료(도착) 시각은 시작(출발) 시각보다 빠를 수 없습니다.")


def _manual_schedule_fields(
    db: Session, req: ScheduleCreateRequest, travel, total_days: int,
) -> tuple[int, dict]:
    """ScheduleCreateRequest → (day_no, schedule_dao.create 인자 dict). kind별 검증·좌표·day_no 계산.

    visit은 요청의 day_no를, train은 출발일(dep_date)로 day_no를 계산한다.
    """
    if req.kind == "visit":
        if req.day_no is None or not req.title or req.latitude is None or req.longitude is None:
            raise BadRequestException("장소는 일자(day_no)·이름·좌표(위도·경도)가 필요합니다.")
        if req.day_no > total_days:
            raise BadRequestException("여행 기간을 벗어난 일자입니다.")
        end_time = req.end_time or req.start_time
        _validate_time_order(req.start_time, end_time)
        return req.day_no, {
            "kind": "visit", "title": req.title,
            "start_time": req.start_time, "end_time": end_time,
            "latitude": req.latitude, "longitude": req.longitude, "image_url": req.image_url,
        }

    # kind == "train"
    if not (req.train_no and req.train_grade and req.dep_station and req.arr_station):
        raise BadRequestException("기차는 열차번호·등급·출발역·도착역이 필요합니다.")
    if req.dep_date is None or req.end_time is None:
        raise BadRequestException("기차는 출발일·출발시각·도착시각이 필요합니다.")
    if not (travel.start_date <= req.dep_date <= travel.end_date):
        raise BadRequestException("출발일이 여행 기간을 벗어났습니다.")
    # arr_date는 받아도 검증·저장하지 않는다(선택) — day_no는 출발일 기준이고 자정 넘김은 미지원.
    _validate_time_order(req.start_time, req.end_time)
    coord = _station_coords(db, req.dep_station, None)
    if coord is None:
        raise BadRequestException("출발역 좌표를 찾을 수 없습니다.")
    lat, lng = coord
    day_no = (req.dep_date - travel.start_date).days + 1
    return day_no, {
        "kind": "train",
        "title": f"{req.train_grade} {req.train_no} {req.dep_station}→{req.arr_station}",
        "train_no": req.train_no, "train_grade": req.train_grade,
        "dep_station": req.dep_station, "arr_station": req.arr_station,
        "car_no": req.car_no, "seat_no": req.seat_no,
        "start_time": req.start_time, "end_time": req.end_time,
        "latitude": lat, "longitude": lng,
    }


def _travel_title(region: str | None, start: date, end: date, fallback: str | None) -> str:
    """여행 제목 = '<지역> N박 N일 여행'. 지역은 도착역명이라 끝의 '역' 접미사를 뗀다(부산역→부산).

    지역명이 없으면 플랜 제목(fallback)→'여행' 순으로 대체한다. 당일 여행이면 'N박' 없이 표기.

    지역명·플랜 제목은 길이 제한이 느슨해(요청 region은 100자까지) 그대로 쓰면 title 컬럼
    (varchar 100)을 넘겨 저장이 깨진다. 기간 표기는 남기고 지역명 쪽을 잘라 한도에 맞춘다.
    """
    place = region.removesuffix("역") if region else None
    if not place:
        return (fallback or "여행")[:_TITLE_MAX]
    days = (end - start).days + 1
    nights = days - 1
    suffix = " 당일 여행" if nights <= 0 else f" {nights}박 {days}일 여행"
    return f"{place[:_TITLE_MAX - len(suffix)]}{suffix}"


def _effective_status(start: date, end: date, today: date) -> str:
    """여행 기간과 오늘로 진행 상태를 계산: 시작 전=PLANNED, 기간 내=ONGOING, 종료 후=COMPLETED."""
    if today < start:
        return "PLANNED"
    if today > end:
        return "COMPLETED"
    return "ONGOING"


def _day_bounds(segments) -> tuple[dict, dict]:
    """일자별 (첫 일정 시작시각, 마지막 비-숙소 종료시각) — 숙소 체크인/아웃 시각 추정용."""
    first: dict[int, datetime] = {}
    last: dict[int, datetime] = {}
    for s in segments:
        if s.start_time is not None:
            cur = first.get(s.day_no)
            first[s.day_no] = s.start_time if cur is None else min(cur, s.start_time)
        if s.end_time is not None and s.kind != "lodging":
            cur = last.get(s.day_no)
            last[s.day_no] = s.end_time if cur is None else max(cur, s.end_time)
    return first, last


def _schedule_fields(db: Session, seg, day_first: dict, day_last: dict, fallback) -> dict | None:
    """세그먼트 → schedule_dao.create 인자 dict(kind별). 저장 불가면 None."""
    if seg.kind == "visit" and seg.place is not None:
        p = seg.place
        st = seg.start_time.time() if seg.start_time else _DEFAULT_TIME
        en = seg.end_time.time() if seg.end_time else st
        return {
            "kind": "visit", "title": p.name, "start_time": st, "end_time": en,
            "latitude": p.lat, "longitude": p.lng, "image_url": p.image_url,
        }

    if seg.kind == "train" and seg.train is not None:
        t = seg.train
        coord = _station_coords(db, t.dep_station, fallback)
        if coord is None:  # 출발역 좌표 미상 → (0,0) 가짜 핀 대신 이 열차 세그먼트는 저장 생략
            return None
        no = t.train_no.lstrip("0") or t.train_no
        lat, lng = coord
        st = seg.start_time.time() if seg.start_time else _DEFAULT_TIME
        en = seg.end_time.time() if seg.end_time else st
        return {
            "kind": "train",
            "title": f"{t.grade} {no} {t.dep_station}→{t.arr_station}",  # 표시용 폴백
            "train_no": no, "train_grade": t.grade,
            "dep_station": t.dep_station, "arr_station": t.arr_station,
            "start_time": st, "end_time": en, "latitude": lat, "longitude": lng,
            "image_url": None,
        }

    if seg.kind == "lodging" and seg.lodging is not None:
        lg = seg.lodging
        checkin = day_last.get(seg.day_no)
        checkout = day_first.get(seg.day_no + 1)
        st = checkin.time() if checkin else _LODGING_CHECKIN
        en = checkout.time() if checkout else _LODGING_CHECKOUT
        return {
            "kind": "lodging", "title": lg.name, "start_time": st, "end_time": en,
            "latitude": lg.lat, "longitude": lg.lng, "image_url": lg.image_url,
        }

    return None


def _station_coords(db: Session, name: str, fallback) -> tuple[float, float] | None:
    """기차 출발역명 → (위도, 경도). '서울'처럼 '역' 접미사가 없으면 붙여 재조회, 그래도 없으면 fallback.

    셋 다 실패하면 None을 반환한다 — (0,0) 같은 실좌표로 폴백하면 지도에 가짜 위치로 찍히므로,
    호출부가 그 세그먼트를 저장 생략하도록 명시적 실패 신호를 준다.
    """
    for candidate in (name, f"{name}역"):
        coord = station_dao.coord_by_name(db, candidate)
        if coord is not None:
            return coord
    return fallback  # 유효 좌표거나 None(미상)


def _selfcheck() -> None:
    """_snap_to_schedule 자가 검증 — `python -m services.travel_service` 로 실행."""
    import io as _io
    from types import SimpleNamespace

    from PIL import Image

    def photo(lat: float | None = None, lng: float | None = None) -> bytes:
        """GPS EXIF를 심은(또는 안 심은) 최소 JPEG 바이트."""
        buf = _io.BytesIO()
        image = Image.new("RGB", (8, 8))
        if lat is None:
            image.save(buf, "JPEG")
        else:
            exif = Image.Exif()
            def dms(v):  # 도/분/초 3쌍 — _extract_photo_meta 의 to_decimal 이 읽는 형식
                d = int(abs(v)); m = int((abs(v) - d) * 60); s = (abs(v) - d - m / 60) * 3600
                return (float(d), float(m), round(s, 2))
            exif.get_ifd(0x8825).update({
                1: "N" if lat >= 0 else "S", 2: dms(lat),
                3: "E" if lng >= 0 else "W", 4: dms(lng),
            })
            image.save(buf, "JPEG", exif=exif)
        return buf.getvalue()

    seoul = SimpleNamespace(schedule_idx=1, latitude=37.5665, longitude=126.9780)   # 서울역
    busan = SimpleNamespace(schedule_idx=2, latitude=35.1152, longitude=129.0423)   # 부산역
    plan = [seoul, busan]

    # 부산에서 찍은 사진은 서울이 아니라 부산 일정에 붙는다(반대도 마찬가지).
    assert _snap_to_schedule(photo(35.1595, 129.0600), plan) == 2   # 부산 시내
    assert _snap_to_schedule(photo(37.5512, 126.9882), plan) == 1   # 남산

    # 후보가 없거나 GPS가 없으면 매핑하지 않는다(NULL → 영상 마지막 지점).
    assert _snap_to_schedule(photo(35.1595, 129.0600), []) is None
    assert _snap_to_schedule(photo(), plan) is None
    assert _snap_to_schedule(b"not an image", plan) is None

    # 아무리 멀어도 가장 가까운 쪽을 고른다(거리 상한을 두지 않는다는 결정의 검증).
    assert _snap_to_schedule(photo(48.8584, 2.2945), plan) == 1     # 파리 → 그나마 서울
    print("travel_service selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
