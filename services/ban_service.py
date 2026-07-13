"""차단 서비스 — 트랜잭션(commit)은 이 레이어가 소유한다.

단방향 차단이다: 내가 차단하면 그 사람의 릴스·댓글이 **나에게만** 안 보인다(상대는 평소대로).
좋아요와 같은 이유로 POST(차단)/DELETE(해제) 모두 멱등이다.
"""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.exceptions.custom import BadRequestException, NotFoundException
from databases.daos import ban_dao, user_dao
from schemas.ban_schema import BlockedUser


def block_user(db: Session, user, target_user_idx: int) -> None:
    """사용자 차단. 이미 차단했으면 그대로 둔다."""
    if target_user_idx == user.user_idx:
        raise BadRequestException("자기 자신은 차단할 수 없습니다.")
    if user_dao.get_by_idx(db, target_user_idx) is None:
        raise NotFoundException("사용자를 찾을 수 없습니다.")

    if ban_dao.get(db, user.user_idx, target_user_idx) is not None:
        return
    try:
        ban_dao.create(db, user.user_idx, target_user_idx)
        db.commit()
    except IntegrityError:
        db.rollback()  # 동시 요청 경합 — 상대가 이미 넣었다. 이미 차단 상태이므로 성공.


def unblock_user(db: Session, user, target_user_idx: int) -> None:
    """차단 해제. 차단 안 한 상대면 아무것도 안 한다."""
    ban = ban_dao.get(db, user.user_idx, target_user_idx)
    if ban is None:
        return
    ban_dao.delete(db, ban)
    db.commit()


def list_blocked(db: Session, user) -> list[BlockedUser]:
    """내가 차단한 사용자 목록 (최근 차단순)."""
    return [
        BlockedUser(user_idx=idx, nickname=nickname)
        for idx, nickname in ban_dao.list_blocked(db, user.user_idx)
    ]
