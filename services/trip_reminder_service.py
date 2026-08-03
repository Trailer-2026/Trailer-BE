"""여행 D-1 알림 서비스 — '여행이 하루 남았습니다'를 매일 자정(KST)에 보낸다.

배치 컨텍스트라 요청 스코프가 아닌 자체 세션을 연다(train_stop_service와 같은 성격).
main.py의 일일 루프가 이 함수를 호출한다.

중복 발송은 notification_log로 막는다 — 여행 1건당 D-1 알림은 평생 1회다. 덕분에
서버 재시작·시작 시 보정 실행을 몇 번 해도 사용자에게 같은 알림이 두 번 가지 않는다.
"""
import logging
from datetime import timedelta

from databases.daos import travel_dao
from databases.database import SessionLocal
from services import push_service
from utils.timezone import KST, now_kst

logger = logging.getLogger(__name__)

# 자정 직후 다시 계산돼 0초 대기가 반복되는 걸 막는 하한.
_MIN_SLEEP_SEC = 60


def send_d1_reminders() -> int:
    """내일(KST) 출발하는 여행의 소유자에게 D-1 알림을 보내고 발송 건수를 반환한다.

    이미 보낸 여행은 push_service.notify_trip_d1이 걸러낸다(멱등). 개별 발송 실패도
    거기서 삼키므로 한 사람 때문에 나머지가 밀리지 않는다.
    """
    target = now_kst().date() + timedelta(days=1)
    db = SessionLocal()
    try:
        travels = travel_dao.list_starting_on(db, target)
        return sum(
            1 for travel in travels
            if push_service.notify_trip_d1(db, travel.user_idx, travel)
        )
    finally:
        db.close()


def seconds_until_next_midnight_kst() -> float:
    """다음 KST 자정까지 남은 초. 최소 _MIN_SLEEP_SEC은 보장한다."""
    now = now_kst()
    next_midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=KST,
    )
    return max(_MIN_SLEEP_SEC, (next_midnight - now).total_seconds())
