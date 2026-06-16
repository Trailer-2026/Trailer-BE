import logging
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from config import Config
from databases.database import get_db
from databases.daos import user_dao
from core.exceptions.custom import UnauthorizedException

logger = logging.getLogger(__name__)

SECRET_KEY = Config.read('jwt', 'secret_key')
ALGORITHM = Config.read('jwt', 'algorithm', default='HS256')
ACCESS_TOKEN_EXPIRE_MINUTES = int(Config.read('jwt', 'access_token_expire_minutes', default='60'))
REFRESH_TOKEN_EXPIRE_DAYS = int(Config.read('jwt', 'refresh_token_expire_days', default='14'))

bearer_scheme = HTTPBearer(auto_error=False)


def _create_token(user_idx: int, token_type: str, expires_delta: timedelta, extra: dict = None) -> tuple[str, datetime]:
    """JWT를 생성해 (token, expires_at)을 반환한다. expires_at은 토큰의 exp와 동일하다."""
    now = datetime.now(timezone.utc)
    expires_at = now + expires_delta
    payload = {
        "sub": str(user_idx),
        "type": token_type,
        "iat": now,
        "exp": expires_at,
    }
    if extra:
        payload.update(extra)
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    return token, expires_at


def create_access_token(user_idx: int) -> str:
    token, _ = _create_token(user_idx, "access", timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    return token


def create_refresh_token(user_idx: int) -> tuple[str, str, datetime]:
    """refresh 토큰 생성 → (token, jti, expires_at).

    jti(고유 ID)를 페이로드에 담아, DB 화이트리스트와 대조해 무효화/회전이 가능하게 한다.
    expires_at은 토큰의 exp와 동일한 값이라 DB 정리 기준과 토큰 만료가 어긋나지 않는다.
    """
    jti = str(uuid.uuid4())
    token, expires_at = _create_token(
        user_idx, "refresh", timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS), extra={"jti": jti}
    )
    return token, jti, expires_at


def decode_token(token: str, expected_type: str = "access") -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise UnauthorizedException("토큰이 만료되었습니다.")
    except jwt.InvalidTokenError:
        raise UnauthorizedException("유효하지 않은 토큰입니다.")

    if payload.get("type") != expected_type:
        raise UnauthorizedException("유효하지 않은 토큰입니다.")
    return payload


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
):
    if credentials is None:
        raise UnauthorizedException("인증 정보가 없습니다.")

    payload = decode_token(credentials.credentials, expected_type="access")
    user = user_dao.get_by_idx(db, int(payload["sub"]))
    if not user:
        raise UnauthorizedException("사용자를 찾을 수 없습니다.")
    return user
