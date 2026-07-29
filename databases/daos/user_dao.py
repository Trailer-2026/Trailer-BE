from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.sql import func
from databases.models.user import User


def get_by_provider(db: Session, provider: str, provider_id: str):
    return db.query(User).filter(
        User.provider == provider,
        User.provider_id == provider_id,
        User.deleted_at.is_(None)
    ).first()


def get_by_idx(db: Session, user_idx: int):
    return db.query(User).filter(
        User.user_idx == user_idx,
        User.deleted_at.is_(None)
    ).first()


def lock_by_idx(db: Session, user_idx: int):
    """user 행을 FOR UPDATE로 잠근다 (soft-delete 제외).

    '예정 여행은 1개만' 검사~생성을 사용자별로 직렬화하기 위한 락 — 동시 생성 요청이
    둘 다 검사를 통과해 여행이 2개 만들어지는 경합을 막는다. 커밋 시 해제된다.
    SQLite(노션 export)에선 FOR UPDATE가 무시돼도 무해하다.
    """
    return db.query(User).filter(
        User.user_idx == user_idx,
        User.deleted_at.is_(None),
    ).with_for_update().first()


def create(db: Session, provider: str, provider_id: str, email: Optional[str] = None,
           nickname: Optional[str] = None):
    user = User(provider=provider, provider_id=provider_id, email=email, nickname=nickname)
    db.add(user)
    db.flush()
    return user


def update(db: Session, user: User, *, nickname: Optional[str] = None,
           profile_image: Optional[str] = None) -> User:
    """전달된 필드만 갱신한다(닉네임 / 프로필 사진). user는 현재 세션에 붙어있는 객체."""
    if nickname is not None:
        user.nickname = nickname
    if profile_image is not None:
        user.profile_image = profile_image
    db.flush()
    return user


def soft_delete(db: Session, user_idx: int) -> Optional[User]:
    """회원 탈퇴 — 소프트 삭제(deleted_at). 이미 삭제됐거나 없으면 None.

    (provider, provider_id) 유니크 제약은 deleted_at을 고려하지 않으므로, 탈퇴 표식을 붙여
    유니크 슬롯을 비워 같은 소셜 계정으로 재가입(새 유저 생성)할 수 있게 한다.
    """
    user = db.query(User).filter(
        User.user_idx == user_idx,
        User.deleted_at.is_(None),
    ).first()
    if user is None:
        return None
    user.deleted_at = func.now()
    user.provider_id = f"{user.provider_id}#withdrawn#{user.user_idx}"
    db.flush()
    return user
