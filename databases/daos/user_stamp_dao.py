"""획득한 스탬프(user_stamp) 조회·적립.

다른 DAO와 같이 **flush만 하고 커밋하지 않는다.** 적립과 알림 이력이 한 트랜잭션에 묶여야
하기 때문이다 — 따로 커밋하면 적립만 남고 이력이 없는 상태가 만들어질 수 있고, 그러면
'이미 딴 스탬프'로 판정돼 알림이 영영 안 나간다.

경합(자정 배치 vs 탭 조회)은 (user_idx, type) 유니크가 가른다. 제약이 DEFERRABLE이 아니라
INSERT 시점(flush)에 바로 걸리므로, 진 쪽은 알림을 보내기 전에 IntegrityError를 받는다.
"""
import logging

from sqlalchemy.orm import Session

from databases.models.user_stamp import UserStamp

logger = logging.getLogger(__name__)


def types_of(db: Session, user_idx: int) -> set[str]:
    """이미 획득한 스탬프 종류 집합."""
    rows = (
        db.query(UserStamp.type)
        .filter(UserStamp.user_idx == user_idx, UserStamp.deleted_at.is_(None))
        .all()
    )
    return {r.type for r in rows}


def award(db: Session, user_idx: int, stamp_type: str) -> UserStamp:
    """스탬프를 적립한다(flush만). 이미 있으면 IntegrityError가 올라온다.

    커밋은 서비스가 한다 — 알림 이력과 같은 트랜잭션이어야 하기 때문이다.
    경합에서 진 쪽은 여기 flush에서 걸리므로, 알림을 보내기 전에 걸러진다.
    """
    row = UserStamp(user_idx=user_idx, type=stamp_type)
    db.add(row)
    db.flush()
    return row


def achieved_at_map(db: Session, user_idx: int) -> dict[str, object]:
    """스탬프 종류 → 획득 시각(created_at). 응답의 achieved_at에 그대로 쓴다."""
    rows = (
        db.query(UserStamp.type, UserStamp.created_at)
        .filter(UserStamp.user_idx == user_idx, UserStamp.deleted_at.is_(None))
        .all()
    )
    return {r.type: r.created_at for r in rows}
