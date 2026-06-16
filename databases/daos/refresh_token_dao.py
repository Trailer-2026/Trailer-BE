import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from databases.models.refresh_token import RefreshToken

logger = logging.getLogger(__name__)


def create(db: Session, user_idx: int, jti: str, expires_at: datetime) -> RefreshToken:
    token = RefreshToken(
        user_idx=user_idx,
        jti=jti,
        expires_at=expires_at,
        revoked=False,
    )
    db.add(token)
    db.flush()
    return token


def get_active_by_jti(db: Session, jti: str):
    """무효화되지 않은(살아있는) refresh 토큰 레코드 조회."""
    return db.query(RefreshToken).filter(
        RefreshToken.jti == jti,
        RefreshToken.revoked.is_(False),
        RefreshToken.deleted_at.is_(None),
    ).first()


def revoke_by_jti(db: Session, jti: str) -> int:
    """단일 refresh 토큰 무효화. 영향받은 행 수 반환."""
    return db.query(RefreshToken).filter(
        RefreshToken.jti == jti,
        RefreshToken.revoked.is_(False),
    ).update({"revoked": True})


def revoke_all_for_user(db: Session, user_idx: int) -> int:
    """해당 사용자의 모든 활성 refresh 토큰 무효화 (모든 기기 로그아웃)."""
    return db.query(RefreshToken).filter(
        RefreshToken.user_idx == user_idx,
        RefreshToken.revoked.is_(False),
    ).update({"revoked": True})


def delete_expired(db: Session, user_idx: Optional[int] = None) -> int:
    """만료된(expires_at < now) refresh 토큰을 삭제. 영향받은 행 수 반환.

    무효화됐지만 아직 만료 전인 토큰은 재사용 탐지를 위해 남겨둔다.
    user_idx가 주어지면 해당 사용자 것만, 없으면 전체를 정리한다.
    """
    query = db.query(RefreshToken).filter(RefreshToken.expires_at < func.now())
    if user_idx is not None:
        query = query.filter(RefreshToken.user_idx == user_idx)
    return query.delete(synchronize_session=False)
