"""열차 탑승 알림(TRAIN_D10M) 자체 점검 — `python tests/test_departure_notification.py`.

인메모리 SQLite에 알림 발송에 필요한 테이블만 세우고 배치를 그대로 돌린다.
프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.

중점은 **역명이 비었을 때**다. dep_station 은 nullable 인데 전에는 여행 제목으로
폴백해서 "부산 3박 4일 여행역에서 출발해요" 가 나갔다. 그 자리는 비워야 한다.
창(10분) 경계와 자정 넘김, 중복 발송 억제도 같이 본다.

station 은 ARRAY·네이티브 ENUM·생성 컬럼을 써서 SQLite 에 못 만든다. 승차권 조회가
역명만 조인하므로 필요한 두 컬럼만 raw DDL 로 세운다.
"""
import sys
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.enums import NotificationType
from databases.models.base import Base
from databases.models.fcm_token import FcmToken
from databases.models.notification import Notification
from databases.models.notification_log import NotificationLog
from databases.models.schedule import Schedule
from databases.models.ticket import Ticket
from databases.models.travel import Travel
from databases.models.user import User
from services import push_service, train_departure_service

TRAVEL_TITLE = "부산 3박 4일 여행"  # 역명 자리에 새어 나오면 안 되는 문자열


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:  # station 대역 — 조인에 쓰이는 두 컬럼만
        conn.execute(text(
            "CREATE TABLE station (station_idx INTEGER PRIMARY KEY, station_name VARCHAR(100))"
        ))
        conn.execute(text("INSERT INTO station VALUES (1, '서울역'), (2, '부산역')"))
    Base.metadata.create_all(engine, tables=[
        m.__table__ for m in
        (User, Travel, Schedule, Ticket, Notification, NotificationLog, FcmToken)
    ])
    return sessionmaker(bind=engine)


def _seed_user(db):
    user = User(user_idx=1, nickname="tester", provider="google", provider_id="sub-1")
    db.add(user)
    db.flush()
    return user


def _seed_train_schedule(db, *, start_time, dep_station, day_no=1,
                         start_date=date(2026, 8, 8), end_date=date(2026, 8, 9)):
    """kind=train 일정 1건 + 그 여행. 출발 일시 = start_date + (day_no-1)일 + start_time."""
    travel = Travel(
        user_idx=1, title=TRAVEL_TITLE, start_date=start_date, end_date=end_date,
        status="PLANNED",
    )
    db.add(travel)
    db.flush()
    schedule = Schedule(
        travel_idx=travel.travel_idx, user_idx=1, day_no=day_no, sequence=0, kind="train",
        title="KTX 101", train_no="101", train_grade="KTX",
        dep_station=dep_station, arr_station="부산",
        start_time=start_time, end_time=time(15, 0), latitude=37.5, longitude=127.0,
    )
    db.add(schedule)
    db.flush()
    return travel, schedule


# ── 1. 문구 조립 ─────────────────────────────────────────────────────────────
def test_body_never_borrows_travel_title():
    """역명이 없으면 그 자리를 통째로 뺀다 — 다른 값으로 메우지 않는다."""
    dep_at = datetime(2026, 8, 8, 12, 10)

    # 기존 정상 케이스는 문자열이 그대로여야 한다(회귀 방지).
    assert push_service._departure_body("서울", dep_at, 10, "KTX 101", None) == \
        "10분 뒤 서울역에서 KTX 101 열차가 출발해요 · 12:10 출발"
    # 직접 입력 승차권은 역명이 이미 '역'으로 끝나고 열차번호가 없다.
    assert push_service._departure_body("서울역", dep_at, 3, None, "3호차 12A") == \
        "3분 뒤 서울역에서 열차가 출발해요 (3호차 12A) · 12:10 출발"

    # 역명 없음 — '에서' 절이 사라질 뿐 나머지는 그대로.
    assert push_service._departure_body(None, dep_at, 10, "KTX 101", None) == \
        "10분 뒤 KTX 101 열차가 출발해요 · 12:10 출발"
    assert push_service._departure_body(None, dep_at, 7, None, None) == \
        "7분 뒤 열차가 출발해요 · 12:10 출발"
    assert push_service._departure_body(None, dep_at, 7, None, "3호차 12A") == \
        "7분 뒤 열차가 출발해요 (3호차 12A) · 12:10 출발"

    # 빈 문자열도 '없음'으로 본다(역명이 공백으로 저장된 옛 데이터).
    assert "역에서" not in push_service._departure_body("", dep_at, 5, None, None)
    print("  OK test_body_never_borrows_travel_title")


# ── 2. 대상 수집 ─────────────────────────────────────────────────────────────
def test_null_station_is_passed_through_as_none():
    """dep_station 이 비면 None 그대로 넘어간다 — 여행 제목으로 바꿔치기하지 않는다."""
    db = _session_factory()()
    _seed_user(db)
    _seed_train_schedule(db, start_time=time(12, 10), dep_station=None)

    found = train_departure_service._departures_between(
        db, datetime(2026, 8, 8, 12, 0), datetime(2026, 8, 8, 12, 10)
    )
    assert len(found) == 1, found
    assert found[0]["dep_station"] is None, f"None 이어야 한다: {found[0]['dep_station']!r}"
    assert found[0]["train_label"] == "KTX 101"
    assert found[0]["minutes_left"] == 10
    print("  OK test_null_station_is_passed_through_as_none")


def test_window_boundaries():
    """(after, until] — 이미 떠난 열차는 빼고, 정확히 10분 뒤 출발은 넣는다."""
    after, until = datetime(2026, 8, 8, 12, 0), datetime(2026, 8, 8, 12, 10)
    cases = [
        (time(11, 59), False, "이미 떠남"),
        (time(12, 0), False, "경계 열림 — after 와 같으면 제외"),
        (time(12, 1), True, "창 안"),
        (time(12, 10), True, "경계 닫힘 — until 과 같으면 포함"),
        (time(12, 11), False, "아직 이름"),
    ]
    for start_time, expected, why in cases:
        db = _session_factory()()
        _seed_user(db)
        _seed_train_schedule(db, start_time=start_time, dep_station="서울")
        got = bool(train_departure_service._departures_between(db, after, until))
        assert got is expected, f"{start_time} → {got} (기대 {expected}: {why})"
    print("  OK test_window_boundaries")


def test_window_crossing_midnight():
    """창이 자정을 넘으면 출발 날짜가 이틀에 걸친다 — 둘 다 훑어야 한다."""
    db = _session_factory()()
    _seed_user(db)
    # 여행 2일차 00:03 출발 = 8/9 00:03
    _seed_train_schedule(db, start_time=time(0, 3), dep_station="서울", day_no=2)

    found = train_departure_service._departures_between(
        db, datetime(2026, 8, 8, 23, 55), datetime(2026, 8, 9, 0, 5)
    )
    assert len(found) == 1, f"자정을 넘긴 출발을 놓쳤다: {found}"
    assert found[0]["dep_at"] == datetime(2026, 8, 9, 0, 3)
    assert found[0]["minutes_left"] == 8
    print("  OK test_window_crossing_midnight")


def test_ticket_source_joins_station_name():
    """직접 입력 승차권은 station 을 조인해 '서울역' 형식 역명을 싣는다."""
    db = _session_factory()()
    _seed_user(db)
    db.add(Ticket(
        user_idx=1, dep_station_idx=1, arr_station_idx=2,
        dep_date=date(2026, 8, 8), dep_time=time(12, 10),
        arr_date=date(2026, 8, 8), arr_time=time(15, 0), car_no="3", seat_no="12A",
    ))
    db.flush()

    found = train_departure_service._departures_between(
        db, datetime(2026, 8, 8, 12, 0), datetime(2026, 8, 8, 12, 10)
    )
    assert len(found) == 1, found
    assert found[0]["dep_station"] == "서울역"
    assert found[0]["train_label"] is None, "직접 입력엔 열차번호 입력칸이 없다"
    assert found[0]["seat_label"] == "3호차 12A"
    assert found[0]["ticket_idx"] is not None and "travel_idx" not in found[0]
    print("  OK test_ticket_source_joins_station_name")


def test_minutes_left_rounds_up():
    """남은 분은 올림, 최소 1 — 상수 10 을 쓰지 않는다."""
    dep = datetime(2026, 8, 8, 12, 10)
    assert train_departure_service._minutes_left(datetime(2026, 8, 8, 12, 0), dep) == 10
    assert train_departure_service._minutes_left(datetime(2026, 8, 8, 12, 0, 30), dep) == 10
    assert train_departure_service._minutes_left(datetime(2026, 8, 8, 12, 7), dep) == 3
    assert train_departure_service._minutes_left(datetime(2026, 8, 8, 12, 9, 59), dep) == 1
    print("  OK test_minutes_left_rounds_up")


# ── 3. 발송 (end-to-end) ─────────────────────────────────────────────────────
def test_send_reminders_end_to_end():
    """배치를 그대로 돌려 이력에 남은 본문을 확인한다 — 여행 제목이 섞이면 안 된다."""
    factory = _session_factory()
    seed = factory()
    _seed_user(seed)
    _seed_train_schedule(seed, start_time=time(12, 10), dep_station=None)
    seed.commit()

    original = train_departure_service.SessionLocal
    train_departure_service.SessionLocal = factory
    try:
        sent = train_departure_service.send_departure_reminders(
            now=datetime(2026, 8, 8, 12, 0)
        )
        assert sent == 1, f"1건 발송 기대, 실제 {sent}"

        db = factory()
        log = db.query(NotificationLog).one()
        print(f"     발송 본문: {log.body}")
        assert log.type == NotificationType.TRAIN_D10M.value
        assert log.title == "일정알림", "설정 스위치가 둘뿐이라 칩은 기존 것을 쓴다"
        assert TRAVEL_TITLE not in log.body, f"여행 제목이 새어 나왔다: {log.body}"
        assert "역" not in log.body, f"없는 역명이 만들어졌다: {log.body}"
        assert log.body == "10분 뒤 KTX 101 열차가 출발해요 · 12:10 출발"
        assert log.schedule_idx is not None and log.ticket_idx is None
        db.close()

        # 1분 루프가 같은 창을 다시 훑어도 재발송하지 않는다(출발 1건당 1회).
        again = train_departure_service.send_departure_reminders(
            now=datetime(2026, 8, 8, 12, 1)
        )
        assert again == 0, f"중복 발송됐다: {again}건"
        db = factory()
        assert db.query(NotificationLog).count() == 1
        db.close()
    finally:
        train_departure_service.SessionLocal = original
    print("  OK test_send_reminders_end_to_end")


def test_respects_event_alarm_switch():
    """event_alarm 을 끈 사용자에겐 이력도 남기지 않는다."""
    factory = _session_factory()
    seed = factory()
    _seed_user(seed)
    _seed_train_schedule(seed, start_time=time(12, 10), dep_station="서울")
    seed.add(Notification(user_idx=1, event_alarm=False, scenery_alarm=True))
    seed.commit()

    original = train_departure_service.SessionLocal
    train_departure_service.SessionLocal = factory
    try:
        sent = train_departure_service.send_departure_reminders(
            now=datetime(2026, 8, 8, 12, 0)
        )
        assert sent == 0, "수신 거부인데 발송됐다"
        db = factory()
        assert db.query(NotificationLog).count() == 0, "OFF 면 이력도 남기지 않는다"
        db.close()
    finally:
        train_departure_service.SessionLocal = original
    print("  OK test_respects_event_alarm_switch")


def main():
    test_body_never_borrows_travel_title()
    test_null_station_is_passed_through_as_none()
    test_window_boundaries()
    test_window_crossing_midnight()
    test_ticket_source_joins_station_name()
    test_minutes_left_rounds_up()
    test_send_reminders_end_to_end()
    test_respects_event_alarm_switch()
    print("탑승 알림 selfcheck OK")


if __name__ == "__main__":
    main()
