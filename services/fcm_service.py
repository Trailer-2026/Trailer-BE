import logging

from sqlalchemy.orm import Session

from databases.daos import fcm_token_dao
from schemas.fcm_schema import PushResultResponse
from utils import firebase

logger = logging.getLogger(__name__)


def register_token(db: Session, user_idx: int, token: str) -> None:
    """기기 FCM 토큰을 등록한다.

    동일 토큰이 이미 있으면 소유 사용자만 갱신(기기 주인이 바뀐 경우),
    없으면 새로 생성한다.
    """
    existing = fcm_token_dao.get_by_token_including_deleted(db, token)
    if existing:
        # 같은 토큰이 이미 있으면 소유 사용자 갱신(기기 주인 변경). soft-delete된
        # 토큰이면 되살린다 — token UNIQUE 제약 때문에 새로 INSERT할 수 없다.
        existing.user_idx = user_idx
        existing.deleted_at = None
    else:
        fcm_token_dao.create(db, user_idx, token)
    db.commit()


def send_push(
    db: Session, user_idx: int, title: str, body: str, data: dict = None
) -> PushResultResponse:
    """사용자의 모든 기기로 푸시를 발송하고, 죽은 토큰은 정리한다."""
    tokens = fcm_token_dao.get_tokens_by_user(db, user_idx)
    if not tokens:
        return PushResultResponse(sent=0, failed=0)

    sent, failed, dead = firebase.send_multicast(tokens, title, body, data)
    if dead:
        fcm_token_dao.soft_delete_by_tokens(db, dead)
    db.commit()
    return PushResultResponse(sent=sent, failed=failed)
