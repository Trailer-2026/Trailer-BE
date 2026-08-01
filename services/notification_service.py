"""알림 수신 설정 서비스 — 사용자당 1행(notification)의 on/off를 읽고 쓴다.

설정 행은 가입 시점에 만들지 않고 처음 조회·수정할 때 기본값(모두 수신)으로 만든다 —
기존 가입자에게 행을 일괄 생성하는 마이그레이션 없이도 동작하게 하기 위해서다.
"""
from sqlalchemy.orm import Session

from databases.daos import notification_dao, user_dao
from schemas.notification_schema import (
    NotificationResponse,
    NotificationUpdateRequest,
)


def get_settings(db: Session, user) -> NotificationResponse:
    """내 알림 설정 조회. 설정 행이 없으면 기본값(모두 수신)으로 만들어 반환한다."""
    setting = _get_or_create(db, user.user_idx)
    db.commit()
    return _to_response(setting)


def update_settings(
    db: Session, user, req: NotificationUpdateRequest,
) -> NotificationResponse:
    """내 알림 설정 수정 — 보낸 항목만 바꾸고 나머지는 유지한다. 갱신된 설정을 반환한다."""
    setting = _get_or_create(db, user.user_idx)
    # 아무 항목도 안 보내면 바꿀 게 없다 — 에러 대신 현재 상태를 그대로 돌려준다(멱등).
    notification_dao.update(db, setting, **req.model_dump(exclude_unset=True, exclude_none=True))
    db.commit()
    return _to_response(setting)


def _get_or_create(db: Session, user_idx: int):
    """설정 행을 가져오고, 없으면 기본값으로 만든다.

    없을 때만 user 행을 잠그고 다시 확인한다 — 첫 요청이 동시에 둘 오면 양쪽이 insert 해
    user_idx UNIQUE 제약에 걸리는 경합을 막는다(커밋 시 해제). 이미 있으면 락 없이 끝난다.
    """
    setting = notification_dao.get_by_user(db, user_idx)
    if setting is not None:
        return setting
    user_dao.lock_by_idx(db, user_idx)
    return notification_dao.get_by_user(db, user_idx) or notification_dao.create(db, user_idx)


def _to_response(setting) -> NotificationResponse:
    return NotificationResponse(
        event_alarm=setting.event_alarm,
        scenery_alarm=setting.scenery_alarm,
    )
