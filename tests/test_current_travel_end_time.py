"""홈 여행 카드 종료 시각 자체 점검 — `python tests/test_current_travel_end_time.py`."""
import sys
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from databases.daos import schedule_dao, travel_dao
from databases.models.base import Base
from databases.models.schedule import Schedule
from databases.models.travel import Travel
from databases.models.travel_image import TravelImage
from databases.models.travel_like import TravelLike
from databases.models.user import User
from services import travel_service
from utils.timezone import KST


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            t.__table__
            for t in (User, Travel, Schedule, TravelImage, TravelLike)
        ],
    )
    db = sessionmaker(bind=engine)()
    db.add(User(user_idx=1, nickname="owner", provider="google", provider_id="p1"))
    db.commit()
    return db


def _travel(db, *, title: str, start: date, end: date):
    travel = travel_dao.create(
        db,
        user_idx=1,
        title=title,
        start_date=start,
        end_date=end,
    )
    db.commit()
    return travel


def _schedule(db, travel, *, kind: str, day_no: int, end: time):
    schedule_dao.create(
        db,
        travel_idx=travel.travel_idx,
        user_idx=1,
        day_no=day_no,
        sequence=0,
        kind=kind,
        title="기차" if kind == "train" else "일정",
        start_time=time(9, 0),
        end_time=end,
        latitude=37.0,
        longitude=127.0,
    )
    db.commit()


def test_current_travel_end_time() -> None:
    original_now_kst = travel_service.now_kst
    db = _session()
    owner = db.get(User, 1)
    end_date = date(2026, 8, 16)
    try:
        ended = _travel(
            db,
            title="종료된 여행",
            start=date(2026, 8, 15),
            end=end_date,
        )
        _schedule(db, ended, kind="visit", day_no=2, end=time(18, 0))

        # 종료 시각과 같은 순간까지는 진행 중이다.
        travel_service.now_kst = lambda: datetime(2026, 8, 16, 18, 0, tzinfo=KST)
        assert travel_service.current_travel(db, owner).travel_idx == ended.travel_idx

        # 종료 시각이 지나면 같은 날이어도 현재 여행에서 빠진다.
        travel_service.now_kst = lambda: datetime(2026, 8, 16, 18, 0, 1, tzinfo=KST)
        assert travel_service.current_travel(db, owner) is None
        all_card = next(
            card
            for card in travel_service.all_travels(db, owner).travels
            if card.travel_idx == ended.travel_idx
        )
        assert all_card.status == "COMPLETED"
        assert [card.travel_idx for card in travel_service.past_travels(db, owner).travels] == [
            ended.travel_idx
        ]
        assert travel_service.travel_detail(db, owner, ended.travel_idx).status == "COMPLETED"

        # 마지막 항목이 기차면 도착 시각(end_time)이 여행 종료 시각이다.
        schedule_dao.soft_delete_by_travel(db, ended.travel_idx)
        travel_dao.soft_delete(db, ended)
        db.commit()
        ticket_trip = _travel(
            db,
            title="기차로 끝나는 여행",
            start=end_date,
            end=end_date,
        )
        _schedule(db, ticket_trip, kind="train", day_no=1, end=time(20, 30))
        travel_service.now_kst = lambda: datetime(2026, 8, 16, 20, 30, 1, tzinfo=KST)
        assert travel_service.current_travel(db, owner) is None

        # 종료일 일정이 없으면 기존처럼 다음 날 00:00 전까지 진행 중이다.
        schedule_dao.soft_delete_by_travel(db, ticket_trip.travel_idx)
        travel_dao.soft_delete(db, ticket_trip)
        db.commit()
        empty_trip = _travel(
            db,
            title="종료 시각 없는 여행",
            start=end_date,
            end=end_date,
        )
        travel_service.now_kst = lambda: datetime(2026, 8, 16, 23, 59, 59, tzinfo=KST)
        assert travel_service.current_travel(db, owner).travel_idx == empty_trip.travel_idx

        print("OK: 홈 여행 카드 종료 시각 자체 점검 통과")
    finally:
        travel_service.now_kst = original_now_kst
        db.close()


if __name__ == "__main__":
    test_current_travel_end_time()
