import logging
from typing import Optional

from sqlalchemy.orm import Session
from databases.models.user import User

logger = logging.getLogger(__name__)


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


def create(db: Session, provider: str, provider_id: str, email: Optional[str] = None,
           nickname: Optional[str] = None):
    user = User(provider=provider, provider_id=provider_id, email=email, nickname=nickname)
    db.add(user)
    db.flush()
    return user
