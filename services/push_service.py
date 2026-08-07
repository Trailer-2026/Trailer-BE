"""푸시 알림 발송 서비스 — 수신 설정 확인 → 이력 저장 → FCM 발송을 한 곳에서 처리한다.

알림은 어디까지나 부가 기능이라 실패해도 호출한 쪽(여행 저장, 배치 루프, 풍경 조회)의
동작을 막지 않는다. 그래서 이 모듈의 함수는 예외를 밖으로 내보내지 않고 경고만 남긴다.

읽기(알림 화면 목록·읽음 처리)는 notification_service가 맡는다 — 발송/조회 책임 분리.
"""
import logging
from datetime import timedelta

from sqlalchemy.orm import Session

from core.enums import NotificationType
from databases.daos import notification_dao, notification_log_dao
from databases.database import engine
from databases.models.notification import Notification
from databases.models.notification_log import NotificationLog
from services import fcm_service
from utils.timezone import now_kst

logger = logging.getLogger(__name__)

# 알림 종류 → notification 테이블의 수신 설정 컬럼.
_ALARM_FIELD = {
    NotificationType.TRAVEL_SAVED: "event_alarm",
    NotificationType.TRAVEL_D1: "event_alarm",
    NotificationType.TRAVEL_DELETED: "event_alarm",
    # 탑승 알림도 일정 알림 스위치를 따른다 — 설정 화면의 스위치는 두 개(이벤트/풍경)뿐이라
    # 새 종류를 위해 세 번째를 만들면 앱 설정 화면까지 같이 바꿔야 한다.
    NotificationType.TRAIN_D10M: "event_alarm",
    NotificationType.SCENERY: "scenery_alarm",
}

# 탭해도 열 화면이 없는 종류 — 대상이 이미 삭제돼 조회하면 404다. 어느 여행이었는지는
# 이력(notification_log.travel_idx)에 남기되, 앱이 이동에 쓰는 FCM data에서는 뺀다.
_NO_DEEPLINK = {NotificationType.TRAVEL_DELETED}

# 알림 화면이 종류별 라벨로 쓰는 제목(디자인의 "풍경알림" 칩). OS 푸시의 제목이기도 하다.
_TITLE_TRAVEL = "일정알림"
_TITLE_SCENERY = "풍경알림"


def ensure_tables() -> None:
    """알림 도메인의 두 테이블(설정 notification, 이력 notification_log)을 없으면 만든다.

    이 저장소엔 마이그레이션 도구가 없어 train_stop과 동일하게 자체 provision한다
    (services/train_stop_service._ensure_table 선례). 서버 기동 시 1회 호출.
    설정 테이블까지 여기서 챙기는 이유는, 운영 DB에 수동 DDL을 안 넣으면 알림 설정
    조회가 그대로 500이 나기 때문이다. 이미 있으면(checkfirst) 아무 일도 안 한다.
    """
    # FK로 참조하는 모델(user, travel)이 같은 MetaData에 올라와 있지 않으면 DDL 컴파일이
    # NoReferencedTableError로 죽는다 — 호출 순서에 기대지 않게 여기서 보장한다.
    from databases.models.travel import Travel  # noqa: F401
    from databases.models.user import User  # noqa: F401

    # FK 대상이 되는 schedule·ticket도 같은 이유로 MetaData에 올려 둔다.
    from databases.models.schedule import Schedule  # noqa: F401
    from databases.models.ticket import Ticket  # noqa: F401

    Notification.__table__.create(bind=engine, checkfirst=True)
    NotificationLog.__table__.create(bind=engine, checkfirst=True)
    _ensure_departure_columns()


def _ensure_departure_columns() -> None:
    """이미 만들어져 있는 notification_log에 TRAIN_D10M용 컬럼·인덱스를 덧붙인다.

    위의 create(checkfirst=True)는 **테이블이 이미 있으면 아무것도 하지 않는다** —
    운영 DB엔 notification_log가 이미 있어서 새 컬럼이 영영 안 생긴다. reels의
    region·thumbnail_url과 같은 방식으로 여기서 ALTER를 따로 건다
    (video_service.ensure_reels_columns 선례).
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE notification_log ADD COLUMN IF NOT EXISTS schedule_idx INTEGER "
            "REFERENCES schedule(schedule_idx)"
        ))
        conn.execute(text(
            "ALTER TABLE notification_log ADD COLUMN IF NOT EXISTS ticket_idx INTEGER "
            "REFERENCES ticket(ticket_idx)"
        ))
        # 중복 발송의 최종 방어 — 모델의 __table_args__와 같은 인덱스지만, 테이블이
        # 이미 있으면 create가 안 만들어 주므로 여기서도 건다.
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_log_train_schedule "
            "ON notification_log (user_idx, schedule_idx) WHERE type = 'TRAIN_D10M'"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_log_train_ticket "
            "ON notification_log (user_idx, ticket_idx) WHERE type = 'TRAIN_D10M'"
        ))


def notify(
    db: Session, user_idx: int, ntype: NotificationType, title: str, body: str,
    travel_idx: int | None = None, scenic_spot_idx: int | None = None,
    schedule_idx: int | None = None, ticket_idx: int | None = None,
    record: bool = True,
) -> bool:
    """수신 설정을 확인하고 이력을 남긴 뒤 사용자의 모든 기기로 푸시한다. 발송했으면 True.

    - 설정이 OFF면 이력도 남기지 않고 False (알림 화면에도 안 뜨는 게 자연스럽다).
    - `record=False`면 푸시만 보내고 이력을 남기지 않는다 — 알림 화면 목록에 쌓지 않을
      종류(풍경)를 위한 것이다.
    - 이력을 먼저 커밋해, FCM 호출이 실패해도 알림 화면에는 남게 한다. 즉 False라도
      이력은 남아 있을 수 있고(FCM 실패), 중복 발송 판정은 이 이력을 기준으로 한다 —
      Firebase 장애 시 같은 알림을 다음 주기에 다시 보내지 않는다(at-most-once).
    - 등록된 기기 토큰이 없어도 True다 — 이력이 남아 알림 화면에는 뜬다.
    - 어떤 예외도 올리지 않는다 — 호출한 쪽의 트랜잭션·루프를 깨지 않기 위해서다.
    """
    try:
        if not _is_enabled(db, user_idx, ntype):
            return False

        if record:
            notification_log_dao.create(
                db, user_idx=user_idx, notification_type=ntype.value, title=title, body=body,
                travel_idx=travel_idx, schedule_idx=schedule_idx, ticket_idx=ticket_idx,
            )
            db.commit()

        # data는 앱이 알림을 탭했을 때 어느 화면을 열지 판단하는 딥링크 정보.
        # 해당 없는 키는 빼서 보낸다(FCM data 값은 모두 문자열이라 빈 값과 구분이 안 된다).
        # 값의 문자열 변환은 firebase.send_multicast가 맡는다.
        data = {"type": ntype.value}
        if travel_idx is not None and ntype not in _NO_DEEPLINK:
            data["travel_idx"] = travel_idx
        if scenic_spot_idx is not None:
            data["scenic_spot_idx"] = scenic_spot_idx
        # 직접 입력 승차권은 여행이 없어(travel_idx NULL) 승차권 목록으로 보내야 한다.
        if ticket_idx is not None:
            data["ticket_idx"] = ticket_idx
        result = fcm_service.send_push(db, user_idx, title, body, data=data)

        # 발송 흔적을 남긴다 — 풍경은 이력도 안 남아서 이 로그가 유일한 확인 수단이다.
        # sent=0은 그 사용자의 기기 토큰이 하나도 등록돼 있지 않다는 뜻(에러 아님).
        logger.info(
            "알림 발송 user=%s type=%s sent=%d failed=%d | %s",
            user_idx, ntype.value, result.sent, result.failed, body,
        )
        return True
    except Exception as e:
        logger.warning("알림 발송 실패(무시하고 진행) user=%s type=%s: %s", user_idx, ntype, e)
        db.rollback()
        return False


def notify_travel_saved(db: Session, user_idx: int, travel) -> bool:
    """'일정에 추가되었어요' — 여행이 저장된 직후 발송. 반드시 커밋 이후에 호출한다.

    저장 시점에 이미 출발이 내일이면 D-1 알림도 바로 같이 보낸다. 자정 배치만 믿으면
    이 여행은 D-1 알림을 영영 못 받는다 — 다음 자정(출발 당일 00:00)의 배치는 '내일
    출발'을 찾으므로 이미 지나간 여행이 되기 때문이다.
    """
    sent = notify(
        db, user_idx, NotificationType.TRAVEL_SAVED,
        title=_TITLE_TRAVEL,
        body=f"'{travel.title}'{_josa_i_ga(travel.title)} 일정에 추가되었어요",
        travel_idx=travel.travel_idx,
    )
    if travel.start_date == now_kst().date() + timedelta(days=1):
        notify_trip_d1(db, user_idx, travel)
    return sent


def notify_trip_d1(db: Session, user_idx: int, travel) -> bool:
    """'일정이 하루 남았어요' — 출발 하루 전 발송. 여행당 1회만 나간다(멱등).

    자정 배치와 저장 시점 즉시 발송 두 경로가 같은 여행을 건드릴 수 있어, 이미 보낸
    이력이 있으면 건너뛴다. 이 검사는 빠른 경로일 뿐 경합(check-then-act)에는
    무력해서, 최종 방어는 notification_log의 부분 유니크 인덱스가 맡는다 — 늦게
    INSERT한 쪽이 거기서 걸려 notify 안에서 롤백되고 푸시도 나가지 않는다.
    """
    if notification_log_dao.exists(
        db, user_idx, NotificationType.TRAVEL_D1.value, travel.travel_idx
    ):
        return False
    return notify(
        db, user_idx, NotificationType.TRAVEL_D1,
        title=_TITLE_TRAVEL,
        body=f"'{travel.title}'의 일정이 하루 남았어요",
        travel_idx=travel.travel_idx,
    )


def notify_travel_deleted(db: Session, user_idx: int, travel) -> bool:
    """'일정에서 삭제되었어요' — 여행이 삭제된 직후 발송. 반드시 커밋 이후에 호출한다.

    소프트 삭제라 travel 행 자체는 남아 travel_idx FK도 유효하다. 다만 열면 404이므로
    FCM data에는 travel_idx를 싣지 않는다(_NO_DEEPLINK) — 이력에는 어느 여행이었는지
    남으니 식별은 되고, 앱이 실수로 이동시킬 여지만 없앤다.
    """
    return notify(
        db, user_idx, NotificationType.TRAVEL_DELETED,
        title=_TITLE_TRAVEL,
        body=f"'{travel.title}'{_josa_i_ga(travel.title)} 일정에서 삭제되었어요",
        travel_idx=travel.travel_idx,
    )


def notify_train_departure(
    db: Session, user_idx: int, *, dep_station: str, dep_at, minutes_left: int,
    train_label: str | None = None, seat_label: str | None = None,
    travel_idx: int | None = None, schedule_idx: int | None = None,
    ticket_idx: int | None = None,
) -> bool:
    """'곧 출발이에요' — 열차 출발 직전 발송. 출발 1건당 1회만 나간다(멱등).

    추천 코스 승차권(schedule)과 직접 입력 승차권(ticket) 두 출처를 모두 받는다 —
    둘 중 하나의 idx만 준다. 1분마다 도는 루프가 같은 열차를 10번 집으므로 이미 보낸
    이력이 있으면 건너뛴다. 이 검사는 빠른 경로일 뿐이고, 최종 방어는 notification_log의
    부분 유니크 인덱스가 맡는다(D-1과 같은 구조).

    train_label은 추천 코스만 있다('KTX 101') — 직접 입력 승차권엔 열차번호·등급 입력칸이
    없어 None이다. seat_label은 반대로 직접 입력에만 있을 수 있다('3호차 12A').
    """
    if notification_log_dao.exists_for_departure(
        db, user_idx, schedule_idx=schedule_idx, ticket_idx=ticket_idx
    ):
        return False
    return notify(
        db, user_idx, NotificationType.TRAIN_D10M,
        title=_TITLE_TRAVEL,
        body=_departure_body(dep_station, dep_at, minutes_left, train_label, seat_label),
        travel_idx=travel_idx, schedule_idx=schedule_idx, ticket_idx=ticket_idx,
    )


def _departure_body(
    dep_station: str, dep_at, minutes_left: int,
    train_label: str | None, seat_label: str | None,
) -> str:
    """탑승 알림 본문 — "10분 뒤 서울역에서 KTX 101 열차가 출발해요 (3호차 12A) · 12:10 출발".

    역명 표기가 출처마다 다르다 — schedule.dep_station은 '서울', ticket은 station을
    조인해 '서울역'이다. 사용자에게 보이는 문구는 하나여야 하므로 '역'을 붙여 맞춘다.

    분은 상수(10)가 아니라 **실제 남은 시간**을 쓴다. 서버가 잠깐 멈췄다 재개되면 남은
    시간이 10분보다 짧은 열차에도 알림이 나가는데, 그 때 '10분 뒤'라고 하면 3분 남은
    사람을 느긋하게 만든다. 출발 시각을 뒤에 같이 붙이는 것도 같은 이유다.
    """
    station = dep_station if dep_station.endswith("역") else f"{dep_station}역"
    train = f"{train_label} 열차가" if train_label else "열차가"
    body = f"{minutes_left}분 뒤 {station}에서 {train} 출발해요"
    if seat_label:
        body += f" ({seat_label})"
    return f"{body} · {dep_at:%H:%M} 출발"


def _josa_i_ga(word: str) -> str:
    """한글 낱말 뒤에 붙일 주격 조사 '이'/'가'를 고른다 (받침 있으면 '이').

    여행 제목은 사용자가 자유롭게 지어서 '부산 여행'(받침 O)도 '제주도'(받침 X)도 온다.
    한글이 아닌 글자로 끝나면(영문·숫자·이모지) 판정이 불가능해 '가'로 둔다.
    """
    if not word:
        return "가"
    last = word[-1]
    if not ("가" <= last <= "힣"):
        return "가"
    has_jong = (ord(last) - 0xAC00) % 28 != 0
    return "이" if has_jong else "가"


def notify_scenery(db: Session, user, items: list[dict], to_station: str) -> bool:
    """구간별 창밖 관광지 조회 결과로 풍경 알림을 보낸다. 보냈으면 True.

    관광지 판정은 호출 측(scenic_spot_service)이 이미 끝냈고, 여기서는 알림만 맡는다.
    보이는 곳이 없으면 보내지 않는다(빈 알림 방지).

    중복 억제는 하지 않는다 — 조회할 때마다 보낸다. 호출 빈도 조절은 앱 몫이다.
    그래서 **이력도 남기지 않는다**(record=False): 알림 화면에서 풍경은 목록이 아니라
    상단 카드로 따로 뜨고, 그 카드는 조회 응답(based_at·items)으로 그리면 된다. 이력을
    남기면 아무도 읽지 않는 행만 호출 횟수만큼 쌓인다.

    푸시 문구는 구간 기준("지금 OO역 스팟을 지나고 있어요")이지만, 딥링크는 가장 가까운
    관광지를 가리킨다.
    """
    if not items:
        return False
    return notify(
        db, user.user_idx, NotificationType.SCENERY,
        title=_TITLE_SCENERY,
        body=_scenery_body(user.nickname, to_station),
        scenic_spot_idx=items[0].get("scenic_spot_idx"),
        record=False,
    )


def _scenery_body(nickname: str | None, to_station: str) -> str:
    """풍경 알림 본문 — "{닉네임} 님, 지금 {역명} 스팟을 지나고 있어요".

    닉네임은 nullable이라(소셜 로그인 직후 등) 없으면 호칭 없이 문장을 시작한다.
    """
    if nickname:
        return f"{nickname} 님, 지금 {to_station} 스팟을 지나고 있어요"
    return f"지금 {to_station} 스팟을 지나고 있어요"


def _is_enabled(db: Session, user_idx: int, ntype: NotificationType) -> bool:
    """해당 종류의 알림 수신이 켜져 있는지. 설정 행이 없으면 기본값(수신)으로 본다.

    여기서 설정 행을 새로 만들지는 않는다 — 배치 루프 같은 읽기 경로에서 쓰기가
    생기는 걸 피한다. 행 생성은 사용자가 설정 화면에 들어올 때
    notification_service._get_or_create가 맡는다.
    """
    setting = notification_dao.get_by_user(db, user_idx)
    if setting is None:
        return True
    return bool(getattr(setting, _ALARM_FIELD[ntype]))
