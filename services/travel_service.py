"""여행 저장 서비스 — 추천 플랜(캐시)을 Travel + schedule로 영속화한다.

프론트는 plan_id만 보내고(긴 코스 재전송 X), 서버가 plan_cache에서 그 플랜(Itinerary)을 꺼내
세그먼트를 그대로 스케줄로 저장한다. 좌표는 방문지·숙소는 자기 좌표, 기차는 출발역 좌표를
station 테이블에서 채운다. 추천 관광지/숙소 대표 이미지는 schedule.image_url에 저장한다.
숙소는 시각이 없어 'ⓐ 그날 마지막 방문 종료~다음날 첫 일정 시작'으로 잡는다.
"""
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy.orm import Session

from core.exceptions.custom import BadRequestException, NotFoundException
from databases.daos import schedule_dao, station_dao, travel_dao
from schemas.travel_schema import (
    HomeTravelCard,
    TravelDayGroup,
    TravelDetailResponse,
    TravelResponse,
    TravelScheduleItem,
)
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
        title=_travel_title(payload.get("region"), start_date, end_date, it.title),
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
        seq[seg.day_no] = seq.get(seg.day_no, 0)
        schedule_dao.create(
            db, travel_idx=travel.travel_idx, user_idx=user.user_idx,
            day_no=seg.day_no, sequence=seq[seg.day_no], **fields,
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


def travel_detail(db: Session, user, travel_idx: int) -> TravelDetailResponse:
    """여행 1건의 일정표(일자별 타임라인) 상세. 본인 여행이 아니거나 없으면 404."""
    travel = travel_dao.get_by_idx(db, travel_idx)
    if travel is None or travel.user_idx != user.user_idx:
        raise NotFoundException("여행을 찾을 수 없습니다.")

    schedules = schedule_dao.list_by_travel(db, travel_idx)
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
        group.items.append(
            TravelScheduleItem(
                schedule_idx=s.schedule_idx, sequence=s.sequence, kind=s.kind, title=s.title,
                train_no=s.train_no, train_grade=s.train_grade,
                dep_station=s.dep_station, arr_station=s.arr_station,
                start_time=s.start_time, end_time=s.end_time,
                latitude=s.latitude, longitude=s.longitude,
                image_url=s.image_url, memo=s.memo,
            )
        )

    today = datetime.now(_KST).date()
    return TravelDetailResponse(
        travel_idx=travel.travel_idx, title=travel.title,
        start_date=travel.start_date, end_date=travel.end_date, region=travel.region,
        status=_effective_status(travel.start_date, travel.end_date, today),
        days=[groups[k] for k in sorted(groups)],
    )


def _travel_title(region: str | None, start: date, end: date, fallback: str | None) -> str:
    """여행 제목 = '<지역> N박 N일 여행'. 지역은 도착역명이라 끝의 '역' 접미사를 뗀다(부산역→부산).

    지역명이 없으면 플랜 제목(fallback)→'여행' 순으로 대체한다. 당일 여행이면 'N박' 없이 표기.
    """
    place = region.removesuffix("역") if region else None
    if not place:
        return fallback or "여행"
    days = (end - start).days + 1
    nights = days - 1
    if nights <= 0:
        return f"{place} 당일 여행"
    return f"{place} {nights}박 {days}일 여행"


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
