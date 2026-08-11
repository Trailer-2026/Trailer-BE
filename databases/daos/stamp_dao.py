"""마이페이지 스탬프 조건을 세는 집계 조회들.

스탬프는 DB에 적립하지 않고 볼 때마다 계산하므로(services/stamp_service 참조) 여기 함수는
전부 읽기 전용 카운트다. 한 사용자의 데이터만 훑고 마이페이지 진입 때만 도는 조회라
인덱스를 따로 두지 않았다(travel.user_idx·schedule.user_idx·reels.user_idx는 이미 있다).

**'다녀온 여행'의 정의**: travel.status 컬럼은 저장 시 항상 PLANNED이고 전환하는 배치가
없어서(travel_dao 주석 참조) 믿을 수 없다. 그래서 여기서도 조회 API와 같은 규칙 — 종료일이
오늘(KST)보다 이전이면 완료 — 으로 판정한다.
"""
from datetime import date

from sqlalchemy import and_, distinct, func
from sqlalchemy.orm import Session

from databases.models.reels import Reels
from databases.models.schedule import Schedule
from databases.models.station import Station
from databases.models.ticket import Ticket
from databases.models.travel import Travel


def _finished_travels(db: Session, user_idx: int, today: date):
    """그 사용자가 이미 다녀온 여행 쿼리(공통 전제)."""
    return db.query(Travel).filter(
        Travel.user_idx == user_idx,
        Travel.deleted_at.is_(None),
        Travel.end_date < today,
    )


def _finished_schedules(db: Session, user_idx: int, today: date):
    """다녀온 여행에 속한 일정 항목 쿼리(공통 전제)."""
    return (
        db.query(Schedule)
        .join(Travel, Schedule.travel_idx == Travel.travel_idx)
        .filter(
            Schedule.user_idx == user_idx,
            Schedule.deleted_at.is_(None),
            Travel.deleted_at.is_(None),
            Travel.end_date < today,
        )
    )


def _past_tickets(db: Session, user_idx: int, today: date):
    """이미 출발한 직접 입력 승차권 쿼리(공통 전제)."""
    return db.query(Ticket).filter(
        Ticket.user_idx == user_idx,
        Ticket.deleted_at.is_(None),
        Ticket.dep_date < today,
    )


def count_train_rides(db: Session, user_idx: int, today: date) -> int:
    """다녀온 기차 탑승 건수 — 추천 코스 일정과 직접 입력 승차권을 **합쳐** 센다.

    승차권만 저장하고 여행은 만들지 않은 사용자도 기차를 탄 것이므로 두 갈래를 모두 본다
    (탑승 알림이 두 소스를 다 훑는 것과 같은 이유). 두 소스를 합쳐 내려주지 않는다는 원칙은
    목록 조회 얘기고, 여기선 '탔는지'만 알면 된다.
    """
    from_schedule = _finished_schedules(db, user_idx, today).filter(
        Schedule.kind == "train"
    ).count()
    return from_schedule + _past_tickets(db, user_idx, today).count()


def count_recommended_travels(db: Session, user_idx: int, today: date) -> int:
    """AI 추천 코스로 저장해 다녀온 여행 수. source 컬럼이 생기기 전 여행은 null이라 빠진다."""
    return _finished_travels(db, user_idx, today).filter(
        Travel.source == "RECOMMEND"
    ).count()


def finished_regions(db: Session, user_idx: int, today: date) -> list[str]:
    """다녀온 여행의 대표 지역 목록(정규화 전 원본).

    region은 자유 문자열이라 '대전'과 '대전역'이 섞여 들어온다. 같은 도시로 묶는 규칙은
    표기를 아는 서비스(stamp_service._city)가 맡고, 여기선 값만 꺼낸다.
    """
    rows = (
        _finished_travels(db, user_idx, today)
        .filter(Travel.region.isnot(None))
        .with_entities(Travel.region)
        .all()
    )
    return [r.region for r in rows]


def count_visited_attractions(db: Session, user_idx: int, today: date) -> int:
    """다녀온 여행에서 들른 서로 다른 명소 수(kind=visit).

    같은 곳을 여러 번 가도 1곳이라 장소명으로 distinct한다. 좌표로 묶지 않는 이유는 추천
    코스와 직접 입력이 같은 장소에 미세하게 다른 좌표를 담을 수 있어서다.
    """
    return (
        _finished_schedules(db, user_idx, today)
        .filter(Schedule.kind == "visit")
        .with_entities(func.count(distinct(Schedule.title)))
        .scalar()
    ) or 0


def visited_station_names(db: Session, user_idx: int, today: date) -> list[str]:
    """다녀온 기차역 이름 목록(정규화 전 원본) — 일정의 출발·도착역 + 승차권의 역.

    표기가 두 갈래로 다르다. schedule.dep_station은 '서울', ticket은 station을 조인해
    '서울역'이다. 접미사를 떼 같은 역으로 묶는 건 서비스(stamp_service._station)가 한다.
    """
    trains = (
        _finished_schedules(db, user_idx, today)
        .filter(Schedule.kind == "train")
        .with_entities(Schedule.dep_station, Schedule.arr_station)
        .all()
    )
    names = [name for row in trains for name in row if name]

    # 소프트 삭제 필터를 WHERE가 아니라 ON에 건다 — WHERE에 두면 outer join이 사실상 inner가
    # 돼, 역이 지워진 승차권은 반대쪽 역까지 통째로 빠진다.
    dep = Station.__table__.alias("dep")
    arr = Station.__table__.alias("arr")
    tickets = (
        _past_tickets(db, user_idx, today)
        .outerjoin(dep, and_(
            Ticket.dep_station_idx == dep.c.station_idx, dep.c.deleted_at.is_(None),
        ))
        .outerjoin(arr, and_(
            Ticket.arr_station_idx == arr.c.station_idx, arr.c.deleted_at.is_(None),
        ))
        .with_entities(dep.c.station_name, arr.c.station_name)
        .all()
    )
    return names + [name for row in tickets for name in row if name]


def count_mugunghwa_rides(db: Session, user_idx: int, today: date) -> int:
    """다녀온 여행 중 무궁화호 탑승 건수.

    직접 입력 승차권(ticket)에는 등급 칸이 아예 없어 셀 수 없다 — 추천 코스 일정만 본다.
    등급 표기가 접두어를 달고 오는 경우가 있어 정확히 일치가 아니라 포함으로 본다.
    """
    return (
        _finished_schedules(db, user_idx, today)
        .filter(Schedule.kind == "train", Schedule.train_grade.like("%무궁화%"))
        .count()
    )


def count_my_reels(db: Session, user_idx: int) -> int:
    """내가 만든 영상 수. 렌더가 끝나지 않은 자리표(url이 빈 문자열)는 빼고 센다."""
    return db.query(Reels).filter(
        Reels.user_idx == user_idx,
        Reels.deleted_at.is_(None),
        Reels.url != "",
    ).count()


def finished_travel_periods(db: Session, user_idx: int, today: date) -> list[tuple[date, date]]:
    """다녀온 여행의 (시작일, 종료일) 목록. 계절 판정용."""
    rows = (
        _finished_travels(db, user_idx, today)
        .with_entities(Travel.start_date, Travel.end_date)
        .all()
    )
    return [(r.start_date, r.end_date) for r in rows]
