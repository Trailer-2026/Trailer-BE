from sqlalchemy.orm import Session

from databases.models.notification import Notification


def get_by_user(db: Session, user_idx: int) -> Notification | None:
    """사용자의 알림 설정 1건. 없으면 None (soft-delete 제외)."""
    return (
        db.query(Notification)
        .filter(
            Notification.user_idx == user_idx,
            Notification.deleted_at.is_(None),
        )
        .first()
    )


def create(db: Session, user_idx: int) -> Notification:
    """알림 설정 1건을 기본값(모두 수신)으로 생성. flush만 하고 commit은 서비스가 한다."""
    setting = Notification(user_idx=user_idx, event_alarm=True, scenery_alarm=True)
    db.add(setting)
    db.flush()
    return setting


def update(db: Session, setting: Notification, **fields) -> Notification:
    """보낸 필드만 수정(event_alarm·scenery_alarm). flush만 하고 commit은 서비스가 한다."""
    for name, value in fields.items():
        setattr(setting, name, value)
    db.flush()
    return setting
