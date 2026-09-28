from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from databases.models.fcm_token import FcmToken
from databases.models.user import User


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


def soft_delete_by_user(db: Session, user_idx: int) -> int:
    """사용자의 모든 기기 토큰을 soft delete. 영향받은 행 수 반환. (로그아웃·탈퇴)"""
    return db.query(FcmToken).filter(
        FcmToken.user_idx == user_idx,
        FcmToken.deleted_at.is_(None),
    ).update({"deleted_at": func.now()}, synchronize_session=False)


def soft_delete_beyond_limit(db: Session, user_idx: int, keep_token: str, limit: int) -> int:
    """사용자의 살아 있는 토큰을 최근 등록순 `limit`개만 남기고 soft delete. 지운 수 반환.

    방금 등록한 `keep_token`은 순서와 무관하게 남긴다 — 시각이 같은 행끼리 밀려
    지금 등록한 기기가 지워지는 일을 막는다.

    세기 전에 사용자 행을 잠가 같은 사용자의 등록을 줄 세운다. 안 잠그면 동시에 들어온
    등록들이 서로의 커밋 전 INSERT 를 못 봐 각자 '상한 이내'로 판단하고 함께 넘긴다.
    FOR UPDATE 가 아니라 FOR NO KEY UPDATE 인 이유: fcm_token INSERT 가 FK 검사로
    user 행에 FOR KEY SHARE 를 이미 잡고 있어, FOR UPDATE 면 두 등록이 서로를 기다리다
    교착된다.
    """
    db.query(User.user_idx).filter(
        User.user_idx == user_idx,
    ).with_for_update(key_share=True).first()

    stale =db.query(FcmToken.fcm_token_idx).filter(
        FcmToken.user_idx == user_idx,
        FcmToken.deleted_at.is_(None),
        FcmToken.token != keep_token,
    ).order_by(
        func.coalesce(FcmToken.updated_at, FcmToken.created_at).desc(),
        FcmToken.fcm_token_idx.desc(),
    ).offset(limit - 1).all()
    if not stale:
        return 0
    # 소유자·삭제 여부를 다시 건다 — 고른 뒤 지우기 전에 다른 계정이 그 토큰을 가져갔으면
    # (사용자 잠금은 남의 등록을 못 막는다) 이제 남의 기기라 건드리면 안 된다.
    return db.query(FcmToken).filter(
        FcmToken.fcm_token_idx.in_([idx for (idx,) in stale]),
        FcmToken.user_idx == user_idx,
        FcmToken.deleted_at.is_(None),
    ).update({"deleted_at": func.now()}, synchronize_session=False)


def soft_delete_by_tokens(db: Session, tokens: list[str]) -> int:
    """주어진 토큰들을 soft delete(deleted_at 세팅). 영향받은 행 수 반환."""
    if not tokens:
        return 0
    return db.query(FcmToken).filter(
        FcmToken.token.in_(tokens),
        FcmToken.deleted_at.is_(None),
    ).update({"deleted_at": func.now()}, synchronize_session=False)
