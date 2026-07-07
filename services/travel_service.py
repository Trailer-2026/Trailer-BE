"""여행 저장 서비스 — 추천 플랜(캐시)을 Travel + schedule로 영속화한다.

프론트는 plan_id만 보내고(긴 코스 재전송 X), 서버가 plan_cache에서 그 플랜(Itinerary)을 꺼내
세그먼트를 그대로 스케줄로 저장한다. 좌표는 방문지·숙소는 자기 좌표, 기차는 출발역 좌표를
station 테이블에서 채운다. 추천 관광지/숙소 대표 이미지는 schedule.image_url에 저장한다.
숙소는 시각이 없어 'ⓐ 그날 마지막 방문 종료~다음날 첫 일정 시작'으로 잡는다.
"""
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy.orm import Session

from core.exceptions.custom import BadRequestException
from databases.daos import schedule_dao, station_dao, travel_dao
from schemas.travel_schema import HomeTravelCard, TravelResponse
from utils import plan_cache

_LODGING_CHECKIN = time(21, 0)   # 마지막 방문 종료를 못 구할 때 체크인 기본값
_LODGING_CHECKOUT = time(9, 0)   # 다음날 첫 일정 시작을 못 구할 때 체크아웃 기본값
_DEFAULT_TIME = time(9, 0)       # 방문/기차 시각이 비어 있을 때 안전 기본값
_KST = timezone(timedelta(hours=9))  # 여행 진행 여부는 KST 오늘 기준으로 판단


def save_selected_plan(db: Session, user, plan_id: str) -> TravelResponse:
    """선택한 추천 플랜(plan_id)을 사용자의 여행으로 저장한다. 서비스가 트랜잭션을 소유(commit)."""
    payload = plan_cache.get(plan_id)
    if payload is None:
        raise BadRequestException("추천이 만료되었습니다. 다시 추천받아 주세요.")

    it = payload["itinerary"]
    start_date = datetime.strptime(payload["go_date"], "%Y%m%d").date()
    end_date = datetime.strptime(payload["back_date"], "%Y%m%d").date()
    fallback = payload.get("fallback_coord")  # (lat, lng) — 기차역 좌표 조회 실패 시 대체

    travel = travel_dao.create(
        db, user_idx=user.user_idx,
        title=(it.title or payload.get("region") or "여행"),
        start_date=start_date, end_date=end_date, region=payload.get("region"),
        status="PLANNED",
    )

    day_first, day_last = _day_bounds(it.segments)
    seq: dict[int, int] = {}
    count = 0
    for seg in it.segments:
        fields = _schedule_fields(db, seg, day_first, day_last, fallback)
        if fields is None:
            continue
        title, st_time, en_time, lat, lng, image_url = fields
        seq[seg.day_no] = seq.get(seg.day_no, 0)
        schedule_dao.create(
            db, travel_idx=travel.travel_idx, user_idx=user.user_idx,
            day_no=seg.day_no, sequence=seq[seg.day_no], title=title,
            start_time=st_time, end_time=en_time, latitude=lat, longitude=lng,
            image_url=image_url,
        )
        seq[seg.day_no] += 1
        count += 1

    db.commit()
    return TravelResponse(
        travel_idx=travel.travel_idx, title=travel.title,
        start_date=travel.start_date, end_date=travel.end_date,
        region=travel.region, status=travel.status, schedule_count=count,
    )


def current_travel(db: Session, user) -> HomeTravelCard | None:
    """홈 화면 여행 카드 1건 — 진행 중 우선, 없으면 가장 가까운 예정, 둘 다 없으면 None.

    status 컬럼은 저장 시 항상 PLANNED이고 자동 전환하는 배치가 없으므로, 진행 여부는
    KST 오늘과 여행 기간으로 계산한다(스케줄러 불필요).
    """
    travels = travel_dao.list_by_user(db, user.user_idx)
    today = datetime.now(_KST).date()
    ongoing = [t for t in travels if t.start_date <= today <= t.end_date]
    upcoming = sorted((t for t in travels if t.start_date > today), key=lambda t: t.start_date)
    chosen = ongoing[0] if ongoing else (upcoming[0] if upcoming else None)
    if chosen is None:
        return None
    return HomeTravelCard(
        travel_idx=chosen.travel_idx, title=chosen.title,
        start_date=chosen.start_date, end_date=chosen.end_date,
        status=_effective_status(chosen.start_date, chosen.end_date, today),
        cover_image_url=schedule_dao.cover_image(db, chosen.travel_idx),
    )


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


def _schedule_fields(db: Session, seg, day_first: dict, day_last: dict, fallback):
    """세그먼트 → (title, start_time, end_time, lat, lng, image_url). 저장 불가면 None."""
    if seg.kind == "visit" and seg.place is not None:
        p = seg.place
        st = seg.start_time.time() if seg.start_time else _DEFAULT_TIME
        en = seg.end_time.time() if seg.end_time else st
        return (p.name, st, en, p.lat, p.lng, p.image_url)

    if seg.kind == "train" and seg.train is not None:
        t = seg.train
        coord = _station_coords(db, t.dep_station, fallback)
        if coord is None:  # 출발역 좌표 미상 → (0,0) 가짜 핀 대신 이 열차 세그먼트는 저장 생략
            return None
        no = t.train_no.lstrip("0") or t.train_no
        title = f"{t.grade} {no} {t.dep_station}→{t.arr_station}"
        lat, lng = coord
        st = seg.start_time.time() if seg.start_time else _DEFAULT_TIME
        en = seg.end_time.time() if seg.end_time else st
        return (title, st, en, lat, lng, None)

    if seg.kind == "lodging" and seg.lodging is not None:
        lg = seg.lodging
        checkin = day_last.get(seg.day_no)
        checkout = day_first.get(seg.day_no + 1)
        st = checkin.time() if checkin else _LODGING_CHECKIN
        en = checkout.time() if checkout else _LODGING_CHECKOUT
        return (lg.name, st, en, lg.lat, lg.lng, lg.image_url)

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
