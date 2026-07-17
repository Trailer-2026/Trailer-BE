from datetime import time

from sqlalchemy.orm import Session

from databases.models.schedule import Schedule


def create(
    db: Session, travel_idx: int, user_idx: int, day_no: int, sequence: int,
    title: str, start_time: time, end_time: time, latitude: float, longitude: float,
    kind: str = "visit", train_no: str | None = None, train_grade: str | None = None,
    dep_station: str | None = None, arr_station: str | None = None,
    image_url: str | None = None, memo: str | None = None,
) -> Schedule:
    """스케줄(일정 항목) 1행 생성. flush만 하고 commit은 서비스가 한다."""
    schedule = Schedule(
        travel_idx=travel_idx, user_idx=user_idx, day_no=day_no, sequence=sequence,
        kind=kind, title=title, train_no=train_no, train_grade=train_grade,
        dep_station=dep_station, arr_station=arr_station,
        start_time=start_time, end_time=end_time,
        latitude=latitude, longitude=longitude, image_url=image_url, memo=memo,
    )
    db.add(schedule)
    db.flush()
    return schedule


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
