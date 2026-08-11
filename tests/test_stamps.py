"""마이페이지 스탬프 자체 점검 — `python tests/test_stamps.py`.

프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.
인메모리 SQLite 라 네트워크·운영 DB 없이 돈다.

지키려는 규칙: 아직 안 끝난 여행은 세지 않는다, 표기가 다른 역·도시를 하나로 묶는다,
승차권과 추천 코스 일정을 합쳐 센다, 계절은 여행 기간이 걸친 달을 모두 본다.
"""
import sys
from datetime import date, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.enums import StampType, TravelSource
from databases.models.base import Base
from databases.models.reels import Reels
from databases.models.schedule import Schedule
from databases.models.ticket import Ticket
from databases.models.travel import Travel
from databases.models.user import User
from services import stamp_service

# station 모델은 Postgres 전용 타입(ENUM 배열)을 써서 SQLite 가 만들지 못한다. 승차권 조인이
# 읽는 건 station_idx·station_name·deleted_at 셋뿐이라 그만큼만 손으로 만든다.
_STATION_DDL = """
CREATE TABLE station (
  station_idx INTEGER PRIMARY KEY,
  station_name VARCHAR(100),
  deleted_at DATETIME
)
"""

USER = 1
TODAY = date.today()
PAST = TODAY - timedelta(days=30)      # 이미 다녀온 여행
FUTURE = TODAY + timedelta(days=30)    # 아직 안 간 여행


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine, tables=[t.__table__ for t in (User, Travel, Schedule, Ticket, Reels)],
    )
    db = sessionmaker(bind=engine)()
    db.execute(text(_STATION_DDL))
    db.add(User(user_idx=USER, nickname="tester", provider="google", provider_id="p1"))
    db.commit()
    return db


def _travel(db, *, start: date, end: date, region=None, source=None) -> Travel:
    travel = Travel(
        user_idx=USER, title="여행", start_date=start, end_date=end,
        region=region, status="PLANNED", source=source,
    )
    db.add(travel)
    db.flush()
    return travel


def _schedule(db, travel, kind, title, **extra):
    db.add(Schedule(
        travel_idx=travel.travel_idx, user_idx=USER, day_no=1, sequence=0,
        kind=kind, title=title, start_time=time(9, 0), end_time=time(10, 0),
        latitude=37.5, longitude=127.0, **extra,
    ))


def _by_type(result) -> dict:
    return {item.type: item for item in result.stamps}


def _check(label, got, want):
    assert got == want, f"{label}: {got} (기대 {want})"


def main():
    db = _session()
    try:
        # ── 아무것도 없으면 전부 잠금 ──────────────────────────────────────
        result = stamp_service.list_stamps(db, USER)
        _check("빈 사용자 달성 수", result.achieved_count, 0)
        _check("칸 수", result.total_count, 9)
        _check("칸 순서", [s.type for s in result.stamps], list(StampType))

        # ── 아직 안 끝난 여행은 세지 않는다 ────────────────────────────────
        upcoming = _travel(db, start=FUTURE, end=FUTURE, region="부산",
                           source=TravelSource.RECOMMEND)
        _schedule(db, upcoming, "train", "KTX", train_grade="무궁화호",
                  dep_station="서울", arr_station="부산")
        _schedule(db, upcoming, "visit", "해운대")
        db.commit()
        stamps = _by_type(stamp_service.list_stamps(db, USER))
        _check("예정 여행은 첫 기차여행 미달성", stamps[StampType.FIRST_TRAIN_TRIP].achieved, False)
        _check("예정 여행은 명소로 안 셈", stamps[StampType.TEN_ATTRACTIONS].progress, 0)

        # ── 다녀온 추천 여행 1건 ──────────────────────────────────────────
        done = _travel(db, start=PAST, end=PAST, region="부산",
                       source=TravelSource.RECOMMEND)
        _schedule(db, done, "train", "무궁화호", train_grade="무궁화호",
                  dep_station="서울", arr_station="부산")
        _schedule(db, done, "visit", "감천문화마을")
        _schedule(db, done, "visit", "감천문화마을")  # 같은 곳 두 번은 1곳
        _schedule(db, done, "lodging", "호텔")        # 숙소는 명소가 아니다
        db.commit()
        stamps = _by_type(stamp_service.list_stamps(db, USER))
        _check("첫 기차여행", stamps[StampType.FIRST_TRAIN_TRIP].achieved, True)
        _check("AI 추천 코스", stamps[StampType.AI_COURSE_DONE].achieved, True)
        _check("무궁화호", stamps[StampType.MUGUNGHWA].achieved, True)
        _check("중복 명소는 1곳", stamps[StampType.TEN_ATTRACTIONS].progress, 1)
        _check("역 2곳", stamps[StampType.TWENTY_STATIONS].progress, 2)

        # ── 직접 입력 승차권도 합쳐 센다(표기가 '서울역'이라 중복이면 안 된다) ──
        db.execute(text(
            "INSERT INTO station (station_idx, station_name) VALUES (1, '서울역'), (2, '대전역')"
        ))
        db.add(Ticket(
            user_idx=USER, dep_station_idx=1, arr_station_idx=2,
            dep_date=PAST, dep_time=time(9, 0), arr_date=PAST, arr_time=time(11, 0),
        ))
        db.commit()
        stamps = _by_type(stamp_service.list_stamps(db, USER))
        _check("'서울'과 '서울역'은 같은 역", stamps[StampType.TWENTY_STATIONS].progress, 3)

        # ── 도시 정규화: '대전역'·'부산광역시'가 '대전'·'부산'으로 묶인다 ────
        for region in ("대전역", "부산광역시", "경기도", "강원특별자치도", "제주"):
            _travel(db, start=PAST, end=PAST, region=region)
        db.commit()
        stamps = _by_type(stamp_service.list_stamps(db, USER))
        # 부산(기존 여행과 같은 도시) + 대전 + 경기 + 강원 + 제주 = 5
        _check("서로 다른 도시 5곳", stamps[StampType.FIVE_CITIES].achieved, True)
        _check("도시 수", stamps[StampType.FIVE_CITIES].progress, 5)

        # ── 계절: 여행 기간이 걸친 달을 모두 본다 ──────────────────────────
        _check("겨울 한 계절뿐", stamp_service._seasons([(date(2025, 1, 5), date(2025, 1, 7))]),
               {"겨울"})
        _check("2월 말~3월 초는 두 계절",
               stamp_service._seasons([(date(2025, 2, 27), date(2025, 3, 2))]), {"겨울", "봄"})
        _check("연말연시도 겨울 하나",
               stamp_service._seasons([(date(2024, 12, 30), date(2025, 1, 2))]), {"겨울"})

        # ── 릴스: 렌더 미완료(url 빈 문자열)는 빼고 센다 ───────────────────
        for i in range(4):
            db.add(Reels(user_idx=USER, url=f"https://x/{i}.mp4", title=f"r{i}"))
        db.add(Reels(user_idx=USER, url="", title="렌더 중"))
        db.commit()
        stamps = _by_type(stamp_service.list_stamps(db, USER))
        _check("렌더 중 릴스는 제외", stamps[StampType.FIVE_REELS].progress, 4)
        db.add(Reels(user_idx=USER, url="https://x/5.mp4", title="r5"))
        db.commit()
        stamps = _by_type(stamp_service.list_stamps(db, USER))
        _check("영상 5개", stamps[StampType.FIVE_REELS].achieved, True)

        # ── 풍경 사진은 촬영 기록을 받는 곳이 없어 항상 잠금 ────────────────
        _check("풍경 사진 잠금", stamps[StampType.SCENERY_PHOTOS].achieved, False)

        # ── progress 는 goal 을 넘지 않는다 ───────────────────────────────
        for item in stamp_service.list_stamps(db, USER).stamps:
            assert item.progress <= item.goal, f"{item.title} progress가 goal 초과"

        print("OK: 마이페이지 스탬프 자체 점검 통과")
    finally:
        db.close()


if __name__ == "__main__":
    main()
