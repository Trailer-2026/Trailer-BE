from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from databases.models.notification_log import NotificationLog


def create(
    db: Session, user_idx: int, type: str, title: str, body: str,
    travel_idx: int | None = None,
) -> NotificationLog:
    """알림 이력 1건 생성. flush만 하고 commit은 서비스가 한다."""
    row = NotificationLog(
        user_idx=user_idx, type=type, title=title, body=body, travel_idx=travel_idx,
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
    """
    query = db.query(NotificationLog).filter(
        NotificationLog.user_idx == user_idx,
        NotificationLog.deleted_at.is_(None),
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
    """안 읽은 알림 수 (soft-delete 제외)."""
    return (
        db.query(NotificationLog)
        .filter(
            NotificationLog.user_idx == user_idx,
            NotificationLog.deleted_at.is_(None),
            NotificationLog.read_at.is_(None),
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


def exists(db: Session, user_idx: int, type: str, travel_idx: int) -> bool:
    """같은 여행에 같은 종류의 알림을 이미 보낸 적이 있는지 — 중복 발송 방지용.

    기간 제한이 없다(D-1은 여행당 평생 1회). soft-delete된 이력도 '보낸 적 있음'으로
    세야 재발송을 막을 수 있어 deleted_at을 필터하지 않는다.
    """
    query = db.query(NotificationLog).filter(
        NotificationLog.user_idx == user_idx,
        NotificationLog.type == type,
        NotificationLog.travel_idx == travel_idx,
    )
    return db.query(query.exists()).scalar()
