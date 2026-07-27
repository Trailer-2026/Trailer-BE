from datetime import time

from sqlalchemy import func
from sqlalchemy.orm import Session

from databases.models.schedule import Schedule


def create(
    db: Session, travel_idx: int, user_idx: int, day_no: int, sequence: int,
    title: str, start_time: time, end_time: time, latitude: float, longitude: float,
    kind: str = "visit", train_no: str | None = None, train_grade: str | None = None,
    dep_station: str | None = None, arr_station: str | None = None,
    car_no: str | None = None, seat_no: str | None = None,
    image_url: str | None = None, memo: str | None = None,
) -> Schedule:
    """스케줄(일정 항목) 1행 생성. flush만 하고 commit은 서비스가 한다."""
    schedule = Schedule(
        travel_idx=travel_idx, user_idx=user_idx, day_no=day_no, sequence=sequence,
        kind=kind, title=title, train_no=train_no, train_grade=train_grade,
        dep_station=dep_station, arr_station=arr_station,
        car_no=car_no, seat_no=seat_no,
        start_time=start_time, end_time=end_time,
        latitude=latitude, longitude=longitude, image_url=image_url, memo=memo,
    )
    db.add(schedule)
    db.flush()
    return schedule


def get_by_idx(db: Session, schedule_idx: int) -> Schedule | None:
    """schedule_idx로 단건 조회 (soft-delete 제외). 편집·삭제용."""
    return db.query(Schedule).filter(
        Schedule.schedule_idx == schedule_idx,
        Schedule.deleted_at.is_(None),
    ).first()


def next_sequence(db: Session, travel_idx: int, day_no: int) -> int:
    """해당 여행·일자에 항목을 뒤에 붙일 sequence = 현재 최대+1 (없으면 0).

    max 조회→insert 사이 경합을 막으려면 호출부가 travel 행을 FOR UPDATE로 잠근 상태여야 한다
    (services.travel_service.add_schedule 참조).
    """
    current = (
        db.query(func.max(Schedule.sequence))
        .filter(
            Schedule.travel_idx == travel_idx,
            Schedule.day_no == day_no,
            Schedule.deleted_at.is_(None),
        )
        .scalar()
    )
    return 0 if current is None else current + 1


def soft_delete(db: Session, schedule: Schedule) -> None:
    """일정 항목 소프트 삭제 (deleted_at 세팅). flush만, commit은 서비스."""
    schedule.deleted_at = func.now()
    db.flush()


def list_by_travel(db: Session, travel_idx: int) -> list[Schedule]:
    """여행의 일정 항목 전체를 타임라인 순서(day_no, sequence)로 조회 (soft-delete 제외)."""
    return (
        db.query(Schedule)
        .filter(
            Schedule.travel_idx == travel_idx,
            Schedule.deleted_at.is_(None),
        )
        .order_by(Schedule.day_no, Schedule.sequence)
        .all()
    )


def list_trains_by_travel(db: Session, travel_idx: int) -> list[Schedule]:
    """여행의 기차(kind=train) 일정만 타임라인 순서(day_no, sequence)로 조회 (soft-delete 제외)."""
    return (
        db.query(Schedule)
        .filter(
            Schedule.travel_idx == travel_idx,
            Schedule.kind == "train",
            Schedule.deleted_at.is_(None),
        )
        .order_by(Schedule.day_no, Schedule.sequence)
        .all()
    )


def cover_image(db: Session, travel_idx: int) -> str | None:
    """여행의 대표 썸네일 — 일정 순서(day_no, sequence)상 이미지가 있는 첫 항목의 image_url. 없으면 None."""
    row = (
        db.query(Schedule.image_url)
        .filter(
            Schedule.travel_idx == travel_idx,
            Schedule.deleted_at.is_(None),
            Schedule.image_url.isnot(None),
        )
        .order_by(Schedule.day_no, Schedule.sequence)
        .first()
    )
    return row[0] if row else None


def cover_images(db: Session, travel_idxs: list[int]) -> dict[int, str]:
    """여행 여러 건의 대표 썸네일을 {travel_idx: image_url}로 일괄 조회 (목록 조회 N+1 회피).

    여행별로 (day_no, sequence)상 이미지가 있는 첫 항목만 남긴다. 이미지가 하나도 없는
    여행은 키 자체가 없다(호출부에서 .get()으로 None 처리).
    """
    if not travel_idxs:
        return {}
    rows = (
        db.query(Schedule.travel_idx, Schedule.image_url)
        .filter(
            Schedule.travel_idx.in_(travel_idxs),
            Schedule.deleted_at.is_(None),
            Schedule.image_url.isnot(None),
        )
        .order_by(Schedule.travel_idx, Schedule.day_no, Schedule.sequence)
        .all()
    )
    covers: dict[int, str] = {}
    for travel_idx, image_url in rows:  # 정렬 순서상 먼저 온 행이 그 여행의 대표
        covers.setdefault(travel_idx, image_url)
    return covers
