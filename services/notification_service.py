"""알림 서비스 — 수신 설정(on/off)과 알림 화면 목록(이력 조회·읽음 처리)을 담당한다.

- 설정: 사용자당 1행(notification). 가입 시점에 만들지 않고 처음 조회·수정할 때 기본값
  (모두 수신)으로 만든다 — 기존 가입자에게 행을 일괄 생성하는 마이그레이션 없이도
  동작하게 하기 위해서다.
- 이력: 발송된 알림(notification_log)을 최신순으로 읽고 읽음 처리한다.
  발송(쓰기)은 services/push_service가 맡는다 — 발송/조회 책임 분리.
"""
from sqlalchemy.orm import Session

from core.exceptions.custom import NotFoundException
from databases.daos import notification_dao, notification_log_dao, user_dao
from schemas.notification_schema import (
    NotificationLogItem,
    NotificationLogListResponse,
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


def list_logs(db: Session, user, limit: int, cursor: int | None) -> NotificationLogListResponse:
    """알림 화면 목록 — 내 알림을 최신순으로, 안 읽은 수와 함께 반환한다.

    limit + 1건을 조회해 다음 페이지가 있는지 판단한다(별도 count 쿼리 없이).
    """
    rows = notification_log_dao.list_by_user(db, user.user_idx, limit + 1, cursor)
    has_more = len(rows) > limit
    rows = rows[:limit]
    return NotificationLogListResponse(
        unread_count=notification_log_dao.unread_count(db, user.user_idx),
        items=[_to_item(r) for r in rows],
        next_cursor=rows[-1].notification_log_idx if has_more and rows else None,
    )


def mark_read(db: Session, user, notification_log_idx: int) -> None:
    """알림 1건 읽음 처리. 내 알림이 아니거나 없으면 404."""
    row = notification_log_dao.get_owned(db, user.user_idx, notification_log_idx)
    if row is None:
        raise NotFoundException("알림을 찾을 수 없습니다.")
    notification_log_dao.mark_read(db, row)
    db.commit()


def mark_all_read(db: Session, user) -> None:
    """안 읽은 알림을 모두 읽음 처리한다. 없어도 에러가 아니다(멱등)."""
    notification_log_dao.mark_all_read(db, user.user_idx)
    db.commit()


def _to_item(row) -> NotificationLogItem:
    return NotificationLogItem(
        notification_log_idx=row.notification_log_idx,
        type=row.type,
        title=row.title,
        body=row.body,
        travel_idx=row.travel_idx,
        ticket_idx=row.ticket_idx,
        is_read=row.read_at is not None,
        created_at=row.created_at,
    )


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
