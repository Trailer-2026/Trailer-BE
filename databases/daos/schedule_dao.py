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
