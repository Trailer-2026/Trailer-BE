from datetime import time

from sqlalchemy.orm import Session

from databases.models.schedule import Schedule


def create(
    db: Session, travel_idx: int, user_idx: int, day_no: int, sequence: int,
    title: str, start_time: time, end_time: time, latitude: float, longitude: float,
    image_url: str | None = None, memo: str | None = None,
) -> Schedule:
    """스케줄(일정 항목) 1행 생성. flush만 하고 commit은 서비스가 한다."""
    schedule = Schedule(
        travel_idx=travel_idx, user_idx=user_idx, day_no=day_no, sequence=sequence,
        title=title, start_time=start_time, end_time=end_time,
        latitude=latitude, longitude=longitude, image_url=image_url, memo=memo,
    )
    db.add(schedule)
    db.flush()
    return schedule
