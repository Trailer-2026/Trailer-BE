from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from core.enums import NotificationType
from databases.models.notification_log import NotificationLog

# 알림 화면 목록에서 감출 종류. 풍경(SCENERY)은 이력을 중복 발송 판정용으로만 남긴다 —
# 화면에서 풍경은 목록의 한 줄이 아니라 상단 카드이고, 그 카드는 구간 조회 응답으로 그린다.
# 목록과 안 읽은 수가 같은 규칙을 써야 "3건인데 열면 1건"이 생기지 않으므로 여기 모아 둔다.
_HIDDEN_TYPES = (NotificationType.SCENERY.value,)


def create(
    db: Session, user_idx: int, notification_type: str, title: str, body: str,
    travel_idx: int | None = None, schedule_idx: int | None = None,
    ticket_idx: int | None = None, stamp_type: str | None = None,
    scenic_spot_idx: int | None = None,
) -> NotificationLog:
    """알림 이력 1건 생성. flush만 하고 commit은 서비스가 한다."""
    row = NotificationLog(
        user_idx=user_idx, type=notification_type, title=title, body=body,
        travel_idx=travel_idx, schedule_idx=schedule_idx, ticket_idx=ticket_idx,
        stamp_type=stamp_type, scenic_spot_idx=scenic_spot_idx,
    )
    db.add(row)
    db.flush()
    return row


def list_by_user(
    db: Session, user_idx: int, limit: int, cursor_idx: int | None = None,
) -> list[NotificationLog]:
    """사용자의 알림을 최신순으로 조회 (soft-delete 제외).

    cursor_idx가 있으면 그보다 작은 PK만 — 커서 페이징이다. PK가 시간순으로
    증가하므로 created_at 동률(같은 배치 발송)에도 순서가 흔들리지 않는다.

    풍경 알림은 제외한다(_HIDDEN_TYPES).
    """
    query = db.query(NotificationLog).filter(
        NotificationLog.user_idx == user_idx,
        NotificationLog.deleted_at.is_(None),
        NotificationLog.type.notin_(_HIDDEN_TYPES),
    )
    if cursor_idx is not None:
        query = query.filter(NotificationLog.notification_log_idx < cursor_idx)
    return (
        query
        .order_by(NotificationLog.notification_log_idx.desc())
        .limit(limit)
        .all()
    )


def unread_count(db: Session, user_idx: int) -> int:
    """안 읽은 알림 수 (soft-delete 제외). 목록과 같은 규칙으로 풍경은 세지 않는다."""
    return (
        db.query(NotificationLog)
        .filter(
            NotificationLog.user_idx == user_idx,
            NotificationLog.deleted_at.is_(None),
            NotificationLog.read_at.is_(None),
            NotificationLog.type.notin_(_HIDDEN_TYPES),
        )
        .count()
    )


def get_owned(db: Session, user_idx: int, notification_log_idx: int) -> NotificationLog | None:
    """본인 알림 단건 조회. 없거나 남의 것이면 None (soft-delete 제외)."""
    return (
        db.query(NotificationLog)
        .filter(
            NotificationLog.notification_log_idx == notification_log_idx,
            NotificationLog.user_idx == user_idx,
            NotificationLog.deleted_at.is_(None),
        )
        .first()
    )


def mark_read(db: Session, row: NotificationLog) -> NotificationLog:
    """알림 1건 읽음 처리. 이미 읽었으면 시각을 덮어쓰지 않는다(멱등)."""
    if row.read_at is None:
        row.read_at = func.now()
        db.flush()
    return row


def mark_all_read(db: Session, user_idx: int) -> int:
    """안 읽은 알림을 모두 읽음 처리하고 처리된 행 수를 반환."""
    return (
        db.query(NotificationLog)
        .filter(
            NotificationLog.user_idx == user_idx,
            NotificationLog.deleted_at.is_(None),
            NotificationLog.read_at.is_(None),
        )
        .update({"read_at": func.now()}, synchronize_session=False)
    )


def exists(db: Session, user_idx: int, notification_type: str, travel_idx: int) -> bool:
    """같은 여행에 같은 종류의 알림을 이미 보낸 적이 있는지 — 중복 발송 방지용.

    기간 제한이 없다(D-1은 여행당 평생 1회). soft-delete된 이력도 '보낸 적 있음'으로
    세야 재발송을 막을 수 있어 deleted_at을 필터하지 않는다.
    """
    query = db.query(NotificationLog).filter(
        NotificationLog.user_idx == user_idx,
        NotificationLog.type == notification_type,
        NotificationLog.travel_idx == travel_idx,
    )
    return db.query(query.exists()).scalar()


def exists_for_departure(
    db: Session, user_idx: int, *,
    schedule_idx: int | None = None, ticket_idx: int | None = None,
) -> bool:
    """그 열차 출발에 대해 TRAIN_D10M을 이미 보냈는지 — 출발 1건당 1회.

    schedule_idx(추천 코스) / ticket_idx(직접 입력) 중 하나만 준다. exists()와 같은
    이유로 soft-delete된 이력도 '보낸 적 있음'으로 센다(지운 알림이 재발송되면 안 된다).
    """
    if (schedule_idx is None) == (ticket_idx is None):
        raise ValueError("schedule_idx 또는 ticket_idx 중 정확히 하나를 줘야 합니다.")
    query = db.query(NotificationLog).filter(
        NotificationLog.user_idx == user_idx,
        NotificationLog.type == NotificationType.TRAIN_D10M.value,
    )
    if schedule_idx is not None:
        query = query.filter(NotificationLog.schedule_idx == schedule_idx)
    else:
        query = query.filter(NotificationLog.ticket_idx == ticket_idx)
    return db.query(query.exists()).scalar()


def sent_scenic_spots(
    db: Session, user_idx: int, *,
    schedule_idx: int | None = None, ticket_idx: int | None = None,
) -> set[int]:
    """그 탑승에서 이미 풍경 알림을 보낸 스팟 idx 집합 — 탑승 1건에서 스팟 1개당 1회.

    풍경 알림은 발송 시각표를 저장하지 않고 1분마다 다시 계산하는 구조라, 같은 구간이
    발송 창 안에서 여러 번 잡힌다. 그 탑승의 '이미 보낸 것'을 한 번에 받아 두고
    파이썬에서 거른다 — 구간마다 exists를 부르면 1분마다 구간 수만큼 왕복한다.

    exists()와 같은 이유로 soft-delete된 이력도 '보낸 적 있음'으로 센다.
    """
    if (schedule_idx is None) == (ticket_idx is None):
        raise ValueError("schedule_idx 또는 ticket_idx 중 정확히 하나를 줘야 합니다.")
    query = db.query(NotificationLog.scenic_spot_idx).filter(
        NotificationLog.user_idx == user_idx,
        NotificationLog.type == NotificationType.SCENERY.value,
        NotificationLog.scenic_spot_idx.isnot(None),
    )
    if schedule_idx is not None:
        query = query.filter(NotificationLog.schedule_idx == schedule_idx)
    else:
        query = query.filter(NotificationLog.ticket_idx == ticket_idx)
    return {row[0] for row in query.all()}
