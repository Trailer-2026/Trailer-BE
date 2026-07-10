from sqlalchemy.orm import Session

from databases.models.notification import Notification


def create(db: Session, user_idx: int, message: str, type: str = "TRAVEL_ADDED") -> Notification:
    """알림 1건 생성. flush만 하고 commit은 서비스가 한다."""
    notification = Notification(user_idx=user_idx, message=message, type=type)
    db.add(notification)
    db.flush()
    return notification


def list_by_user(db: Session, user_idx: int) -> list[Notification]:
    """사용자의 알림을 최신순(created_at 내림차순)으로 조회 (soft-delete 제외)."""
    return (
        db.query(Notification)
        .filter(Notification.user_idx == user_idx, Notification.deleted_at.is_(None))
        .order_by(Notification.created_at.desc())
        .all()
    )
