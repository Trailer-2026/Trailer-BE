from sqlalchemy.orm import Session

from databases.models.ban import Ban
from databases.models.user import User


def get(db: Session, user_idx: int, blocked_user_idx: int) -> Ban | None:
    """차단 관계 1건. 없으면 None."""
    return (
        db.query(Ban)
        .filter(Ban.user_idx == user_idx, Ban.blocked_user_idx == blocked_user_idx)
        .first()
    )


def create(db: Session, user_idx: int, blocked_user_idx: int) -> Ban:
    """차단 1건 생성. flush만 하고 commit은 서비스가 한다."""
    ban = Ban(user_idx=user_idx, blocked_user_idx=blocked_user_idx)
    db.add(ban)
    db.flush()
    return ban


def delete(db: Session, ban: Ban) -> None:
    """차단 해제 = 행 삭제(소프트 삭제 아님 — 유니크 제약과 충돌하고 재차단이 흔하다)."""
    db.delete(ban)
    db.flush()


def blocked_user_idxs(db: Session, user_idx: int) -> list[int]:
    """내가 차단한 사용자 PK 목록 — 댓글·릴스 조회에서 제외할 작성자들."""
    rows = db.query(Ban.blocked_user_idx).filter(Ban.user_idx == user_idx).all()
    return [idx for (idx,) in rows]


def list_blocked(db: Session, user_idx: int) -> list[tuple[int, str | None]]:
    """내가 차단한 사용자 (user_idx, 닉네임) 목록 — 차단 목록 화면용."""
    return (
        db.query(User.user_idx, User.nickname)
        .join(Ban, Ban.blocked_user_idx == User.user_idx)
        .filter(Ban.user_idx == user_idx)
        .order_by(Ban.ban_idx.desc())
        .all()
    )
