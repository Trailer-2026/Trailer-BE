"""마이페이지 스탬프 자체 점검 — `python tests/test_stamps.py`.

프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.
인메모리 SQLite 라 네트워크·운영 DB 없이 돈다. FCM 발송은 스텁으로 갈음한다.

지키려는 규칙: 아직 안 끝난 여행은 세지 않는다(단 사진·릴스는 행동이라 날짜를 안 본다),
표기가 다른 역·도시를 하나로 묶는다,
승차권과 추천 코스 일정을 합쳐 센다, 계절은 여행 기간이 걸친 달을 모두 본다,
**획득 알림은 스탬프당 평생 1회**, 한 번 딴 스탬프는 조건이 어긋나도 유지된다.
"""
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.enums import NotificationType, StampType, TravelSource
from databases.daos import notification_log_dao, stamp_dao
from databases.models.base import Base
from databases.models.notification import Notification
from databases.models.notification_log import NotificationLog
from databases.models.reels import Reels
from databases.models.schedule import Schedule
from databases.models.ticket import Ticket
from databases.models.travel import Travel
from databases.models.travel_image import TravelImage
from databases.models.user import User
from databases.models.user_stamp import UserStamp
from services import fcm_service, stamp_service

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
OTHER = 2      # 승차권만 있고 여행은 없는 사용자
TODAY = date.today()
PAST = TODAY - timedelta(days=30)      # 이미 다녀온 여행
FUTURE = TODAY + timedelta(days=30)    # 아직 안 간 여행


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        User, Travel, Schedule, Ticket, Reels, TravelImage, Notification, NotificationLog,
        UserStamp,
    )])
    db = sessionmaker(bind=engine)()
    db.execute(text(_STATION_DDL))
    db.add(User(user_idx=USER, nickname="tester", provider="google", provider_id="p1"))
    db.commit()
    return db


def _stub_fcm() -> list[dict]:
    """FCM 대신 리스트에 기록한다 — 발송된 푸시의 (제목, 본문, data)."""
    sent: list[dict] = []

    class _Result:  # push_service가 로그에 쓰는 두 필드만 있으면 된다
        sent = 0
        failed = 0

    def fake(db, user_idx, title, body, data=None, image_url=None):
        sent.append({"user_idx": user_idx, "title": title, "body": body, "data": data or {}})
        return _Result()

    fcm_service.send_push = fake
    return sent


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
    original_send = fcm_service.send_push
    db = _session()
    pushes = _stub_fcm()
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

        # ── 여행 사진: 여행이 안 끝났어도 센다(사진은 여행 중에 올린다) ──────
        #    upcoming 은 아직 안 간 여행이다 — 여기 올린 사진도 세야 한다.
        for i in range(9):
            db.add(TravelImage(travel_idx=upcoming.travel_idx, url=f"https://x/p{i}.jpg"))
        db.add(TravelImage(travel_idx=upcoming.travel_idx, url="https://x/gone.jpg",
                           deleted_at=datetime.now()))
        db.commit()
        stamps = _by_type(stamp_service.list_stamps(db, USER))
        _check("지운 사진은 빼고 9장", stamps[StampType.SCENERY_PHOTOS].progress, 9)
        _check("9장이면 아직 잠금", stamps[StampType.SCENERY_PHOTOS].achieved, False)

        db.add(TravelImage(travel_idx=upcoming.travel_idx, url="https://x/p9.jpg"))
        db.commit()
        stamps = _by_type(stamp_service.list_stamps(db, USER))
        _check("사진 10장", stamps[StampType.SCENERY_PHOTOS].achieved, True)

        # ── progress 는 goal 을 넘지 않는다 ───────────────────────────────
        for item in stamp_service.list_stamps(db, USER).stamps:
            assert item.progress <= item.goal, f"{item.title} progress가 goal 초과"

        # ── 알림: 획득한 스탬프마다 한 통씩, 그리고 다시는 안 온다 ───────────
        earned = {s.type for s in stamp_service.list_stamps(db, USER).stamps if s.achieved}
        stamp_pushes = [p for p in pushes if p["data"].get("type") == "STAMP_EARNED"]
        _check("획득 수만큼 푸시", len(stamp_pushes), len(earned))
        _check("푸시가 가리키는 스탬프",
               {p["data"]["stamp_type"] for p in stamp_pushes}, {t.value for t in earned})
        _check("푸시 제목(알림 화면 칩)", {p["title"] for p in stamp_pushes}, {"스탬프알림"})

        logs = db.query(NotificationLog).filter(
            NotificationLog.type == NotificationType.STAMP_EARNED.value
        ).all()
        _check("이력도 같은 수", len(logs), len(earned))
        _check("이력에 스탬프 종류가 남는다",
               {row.stamp_type for row in logs}, {t.value for t in earned})

        before = len(pushes)
        stamp_service.list_stamps(db, USER)
        stamp_service.list_stamps(db, USER)
        _check("다시 조회해도 재발송 없음", len(pushes), before)

        # 배치가 같은 사용자를 또 집어도 마찬가지다(멱등).
        _check("재판정해도 새로 찍히는 것 없음", stamp_service.evaluate(db, USER), [])
        _check("재판정 후에도 푸시 그대로", len(pushes), before)

        # ── 한 번 딴 스탬프는 조건이 어긋나도 유지된다 ─────────────────────
        db.query(Reels).filter(Reels.user_idx == USER).delete(synchronize_session=False)
        db.commit()
        after = _by_type(stamp_service.list_stamps(db, USER))[StampType.FIVE_REELS]
        _check("릴스를 다 지워도 유지", after.achieved, True)
        _check("유지된 칸은 progress도 목표치", after.progress, after.goal)
        assert after.achieved_at is not None, "획득 시각이 있어야 한다"

        # ── 이력 저장이 실패하면 적립도 안 남아야 한다 (다음 재판정에서 재시도) ──
        #    적립만 커밋해 버리면 '이미 딴 스탬프'로 판정돼 알림이 영영 0통이 된다.
        db.query(UserStamp).delete(synchronize_session=False)
        db.query(NotificationLog).delete(synchronize_session=False)
        db.commit()
        db.expunge_all()  # 벌크 삭제라 식별자 맵이 남는다(경고만, 제품 동작과 무관)
        original_create = notification_log_dao.create

        def broken_create(*a, **k):
            raise RuntimeError("이력 INSERT 실패")

        notification_log_dao.create = broken_create
        try:
            _check("이력 실패 시 적립 0건", stamp_service.evaluate(db, USER), [])
            _check("적립도 안 남는다", db.query(UserStamp).count(), 0)
        finally:
            notification_log_dao.create = original_create
        recovered = stamp_service.evaluate(db, USER)
        assert recovered, "복구 후 재판정에서 다시 찍혀야 한다"
        _check("복구 후 적립·이력 수 일치",
               db.query(UserStamp).count(), db.query(NotificationLog).count())

        # ── 수신 거부여도 스탬프는 적립된다(알림·이력만 없다) ──────────────
        db.query(UserStamp).delete(synchronize_session=False)
        db.query(NotificationLog).delete(synchronize_session=False)
        db.add(Notification(user_idx=USER, event_alarm=False, scenery_alarm=False))
        db.commit()
        db.expunge_all()
        off_earned = stamp_service.evaluate(db, USER)
        assert off_earned, "수신 거부여도 적립은 돼야 한다"
        _check("수신 거부면 이력 없음", db.query(NotificationLog).count(), 0)
        _check("그래도 적립은 남음", db.query(UserStamp).count(), len(off_earned))

        # ── 승차권만 있고 여행이 없는 사용자도 배치 대상이다 ────────────────
        #    자정 배치가 여행만 훑으면, 승차권만 저장한 사람은 앱을 열기 전까지
        #    '첫 기차여행'을 못 받는다.
        db.add(User(user_idx=OTHER, nickname="ticket-only", provider="google",
                    provider_id="p2"))
        db.add(Ticket(
            user_idx=OTHER, dep_station_idx=1, arr_station_idx=2,
            dep_date=PAST, dep_time=time(9, 0), arr_date=PAST, arr_time=time(11, 0),
        ))
        db.commit()
        _check("여행이 하나도 없다",
               db.query(Travel).filter(Travel.user_idx == OTHER).count(), 0)
        _check("그래도 배치 대상에 잡힌다",
               OTHER in stamp_dao.user_idxs_with_trip_ending_between(db, PAST, PAST), True)
        ticket_only = _by_type(stamp_service.list_stamps(db, OTHER))
        _check("첫 기차여행이 켜진다", ticket_only[StampType.FIRST_TRAIN_TRIP].achieved, True)
        _check("역 2곳도 센다", ticket_only[StampType.TWENTY_STATIONS].progress, 2)
        # 사진의 주인은 travel 을 조인해 가린다 — 남의 사진이 새면 여기서 잡힌다.
        _check("남의 여행 사진은 안 센다", ticket_only[StampType.SCENERY_PHOTOS].progress, 0)

        print("OK: 마이페이지 스탬프 자체 점검 통과")
    finally:
        db.close()
        fcm_service.send_push = original_send


if __name__ == "__main__":
    main()
