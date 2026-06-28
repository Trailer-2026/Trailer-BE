import logging

from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from databases.models.fcm_token import FcmToken

logger = logging.getLogger(__name__)


def get_by_token(db: Session, token: str):
    return db.query(FcmToken).filter(
        FcmToken.token == token,
        FcmToken.deleted_at.is_(None),
    ).first()


def get_by_token_including_deleted(db: Session, token: str):
    """soft-delete 여부와 무관하게 토큰 행을 조회한다.

    token 컬럼은 DB UNIQUE 제약이 있어 soft-delete된 행도 같은 토큰을 점유한다.
    재등록(upsert) 시 이 행을 되살려야 UNIQUE 충돌을 피할 수 있어, 이 경우에만
    deleted_at 필터 없이 조회한다.
    """
    return db.query(FcmToken).filter(FcmToken.token == token).first()


def get_tokens_by_user(db: Session, user_idx: int) -> list[str]:
    rows = db.query(FcmToken).filter(
        FcmToken.user_idx == user_idx,
        FcmToken.deleted_at.is_(None),
    ).all()
    return [r.token for r in rows]


def create(db: Session, user_idx: int, token: str) -> FcmToken:
    row = FcmToken(user_idx=user_idx, token=token)
    db.add(row)
    db.flush()
    return row


def soft_delete_by_tokens(db: Session, tokens: list[str]) -> int:
    """주어진 토큰들을 soft delete(deleted_at 세팅). 영향받은 행 수 반환."""
    if not tokens:
        return 0
    return db.query(FcmToken).filter(
        FcmToken.token.in_(tokens),
        FcmToken.deleted_at.is_(None),
    ).update({"deleted_at": func.now()}, synchronize_session=False)
