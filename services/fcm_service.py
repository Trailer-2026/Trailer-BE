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
    db: Session, user_idx: int, title: str, body: str, data: dict = None,
    image_url: str | None = None,
) -> PushResultResponse:
    """사용자의 모든 기기로 푸시를 발송하고, 죽은 토큰은 정리한다.

    커밋은 죽은 토큰을 실제로 지웠을 때만 한다. 이 함수는 조회 요청 안에서도 불린다 —
    풍경 알림은 이력을 남기지 않아(push_service.notify record=False)
    GET /api/scenic-spots/nearby의 요청 세션에서 그대로 도는데, 남길 변경이 없는데도
    커밋하면 조회가 쓰기 트랜잭션을 여는 꼴이 된다.
    """
    tokens = fcm_token_dao.get_tokens_by_user(db, user_idx)
    if not tokens:
        return PushResultResponse(sent=0, failed=0)

    sent, failed, dead = firebase.send_multicast(tokens, title, body, data, image_url)
    # 실제로 지워진 행이 있을 때만 커밋한다. 죽은 토큰을 집었어도 UPDATE 가 0행일 수
    # 있다 — 같은 사용자에게 푸시가 동시에 나가면 양쪽이 같은 토큰을 죽은 것으로 보고,
    # 늦은 쪽은 이미 지워진 행을 다시 지우려 해 바꿀 게 없다.
    if dead and fcm_token_dao.soft_delete_by_tokens(db, dead):
        db.commit()
    return PushResultResponse(sent=sent, failed=failed)
