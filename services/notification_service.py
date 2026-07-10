"""알림 서비스 — 데모 범위. 알림 목록 조회와, 다른 서비스가 트랜잭션 안에서 부르는 생성 헬퍼.

생성 헬퍼(notify_travel_added)는 flush만 하고 commit하지 않는다 — 호출한 서비스(travel_service)가
자기 트랜잭션에서 함께 commit해 여행 저장과 알림이 원자적으로 남게 한다.
"""
from sqlalchemy.orm import Session

from databases.daos import notification_dao
from schemas.notification_schema import NotificationItem, NotificationListResponse


def list_for_user(db: Session, user) -> NotificationListResponse:
    """로그인 사용자의 알림을 최신순으로 반환한다(읽기 전용)."""
    rows = notification_dao.list_by_user(db, user.user_idx)
    items = [
        NotificationItem(
            notification_idx=n.notification_idx,
            type=n.type,
            message=n.message,
            created_at=n.created_at,
        )
        for n in rows
    ]
    return NotificationListResponse(items=items)


def notify_travel_added(db: Session, user_idx: int, travel_title: str) -> None:
    """'{여행제목}'이(가) 일정에 추가되었어요 알림을 남긴다(flush만, commit은 호출자)."""
    message = f"'{travel_title}'{_subject_josa(travel_title)} 일정에 추가되었어요"
    notification_dao.create(db, user_idx=user_idx, message=message, type="TRAVEL_ADDED")


def _subject_josa(word: str) -> str:
    """한글 끝 글자 받침 유무로 주격 조사 '이'/'가'를 고른다(받침 있으면 '이')."""
    if not word:
        return "가"
    last = word[-1]
    if "가" <= last <= "힣":
        has_batchim = (ord(last) - 0xAC00) % 28 != 0
        return "이" if has_batchim else "가"
    return "가"  # 한글이 아니면 '가'로 폴백
