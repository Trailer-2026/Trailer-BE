import logging

from sqlalchemy.orm import Session

from databases.daos import user_dao, refresh_token_dao
from databases.models.user import User
from utils import oauth
from utils.nickname import generate_nickname
from core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
)
from core.exceptions.custom import BadRequestException, UnauthorizedException
from schemas.auth_schema import TokenResponse

logger = logging.getLogger(__name__)

_PROVIDER_FETCHERS = {
    "kakao": oauth.fetch_kakao_user,
}


def _issue_tokens(user: User, db: Session) -> TokenResponse:
    """access/refresh 토큰을 발급하고, refresh 토큰을 화이트리스트에 기록한다.

    발급 시 해당 사용자의 만료된 토큰을 함께 정리해 테이블이 무한히 커지는 것을 막는다.
    """
    refresh_token_dao.delete_expired(db, user.user_idx)
    access = create_access_token(user.user_idx)
    refresh, jti, expires_at = create_refresh_token(user.user_idx)
    refresh_token_dao.create(db, user.user_idx, jti, expires_at)
    return TokenResponse(access_token=access, refresh_token=refresh)


def _login_with_social_user(provider: str, social_user: dict, db: Session) -> TokenResponse:
    provider_id = social_user["provider_id"]
    user = user_dao.get_by_provider(db, provider, provider_id)
    if not user:
        # 첫 로그인(회원가입): 랜덤 닉네임 자동 부여(사용자가 나중에 변경 가능).
        # user_dao.create() 내부에서 flush 하므로 여기서 user_idx 사용 가능
        user = user_dao.create(
            db, provider, provider_id, social_user.get("email"),
            nickname=generate_nickname(),
        )

    tokens = _issue_tokens(user, db)
    db.commit()
    return tokens


async def social_login(provider: str, access_token: str, db: Session) -> TokenResponse:
    fetcher = _PROVIDER_FETCHERS.get(provider)
    if fetcher is None:
        raise BadRequestException("지원하지 않는 소셜 제공자입니다.")

    social_user = await fetcher(access_token)
    return _login_with_social_user(provider, social_user, db)


def google_id_token_login(id_token: str, db: Session) -> TokenResponse:
    """구글 id_token을 검증해 로그인 (access_token 방식보다 보안 강화)."""
    social_user = oauth.verify_google_id_token(id_token)
    return _login_with_social_user("google", social_user, db)


def refresh_token(token: str, db: Session) -> TokenResponse:
    """refresh 토큰을 검증·회전(rotation)한다.

    jti가 화이트리스트에 살아있을 때만 재발급하며, 재발급과 동시에 기존 토큰을
    무효화한다. 이미 무효화된 토큰의 재사용은 거부된다.
    """
    payload = decode_token(token, expected_type="refresh")
    jti = payload.get("jti")

    if refresh_token_dao.get_active_by_jti(db, jti) is None:
        raise UnauthorizedException("유효하지 않은 refresh 토큰입니다.")

    user = user_dao.get_by_idx(db, int(payload["sub"]))
    if not user:
        raise UnauthorizedException("사용자를 찾을 수 없습니다.")

    # 회전: 기존 토큰 무효화 후 새 토큰 발급
    refresh_token_dao.revoke_by_jti(db, jti)
    tokens = _issue_tokens(user, db)
    db.commit()
    return tokens


def logout(token: str, db: Session) -> None:
    """전달한 refresh 토큰을 무효화한다. 이미 만료/무효인 토큰이면 조용히 성공 처리."""
    try:
        payload = decode_token(token, expected_type="refresh")
    except UnauthorizedException:
        return  # 이미 못 쓰는 토큰 → 로그아웃은 성공으로 간주(멱등)

    jti = payload.get("jti")
    if jti:
        refresh_token_dao.revoke_by_jti(db, jti)
        db.commit()


def logout_all(user_idx: int, db: Session) -> None:
    """해당 사용자의 모든 refresh 토큰을 무효화한다 (모든 기기 로그아웃)."""
    refresh_token_dao.revoke_all_for_user(db, user_idx)
    db.commit()


def cleanup_expired_tokens(db: Session) -> int:
    """전체 사용자의 만료된 refresh 토큰을 일괄 정리한다 (배치/스케줄러용).

    삭제된 행 수를 반환한다.
    """
    deleted = refresh_token_dao.delete_expired(db)
    db.commit()
    logger.info(f"만료된 refresh 토큰 {deleted}건 정리 완료")
    return deleted
