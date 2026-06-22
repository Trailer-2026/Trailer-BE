import logging

import httpx
import jwt
from jwt import PyJWKClient

from config import Config
from core.exceptions.custom import BadRequestException

logger = logging.getLogger(__name__)

KAKAO_USERINFO_URL = "https://kapi.kakao.com/v2/user/me"

GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

GOOGLE_CLIENT_ID = Config.read('oauth', 'google_client_id')
# JWKS(구글 공개키)는 _jwk_client가 내부적으로 캐싱한다. 생성 시 네트워크 호출은 없음.
_google_jwk_client = PyJWKClient(GOOGLE_CERTS_URL)


async def fetch_kakao_user(access_token: str) -> dict:
    """카카오 access_token으로 사용자 정보 조회 → {provider_id, email}"""
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(KAKAO_USERINFO_URL, headers=headers)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as e:
        logger.warning(f"카카오 사용자 정보 조회 실패: {e}")
        raise BadRequestException("유효하지 않은 소셜 토큰입니다.")

    kakao_account = data.get("kakao_account") or {}
    return {
        "provider_id": str(data.get("id")),
        "email": kakao_account.get("email"),
    }


def verify_google_id_token(id_token: str) -> dict:
    """구글 id_token을 검증(서명/aud/iss/만료) → {provider_id, email}.

    access_token+userinfo 방식과 달리, 이 토큰이 우리 앱(client_id)을 대상으로
    구글이 직접 발급했음을 암호학적으로 검증하므로 토큰 바꿔치기 공격에 안전하다.
    """
    if not GOOGLE_CLIENT_ID:
        logger.error("google_client_id 미설정 — id_token 검증 불가")
        raise BadRequestException("구글 로그인 설정이 누락되었습니다.")

    try:
        signing_key = _google_jwk_client.get_signing_key_from_jwt(id_token)
        payload = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=GOOGLE_CLIENT_ID,
        )
    except jwt.PyJWTError as e:
        logger.warning(f"구글 id_token 검증 실패: {e}")
        raise BadRequestException("유효하지 않은 구글 id_token입니다.")

    if payload.get("iss") not in GOOGLE_ISSUERS:
        logger.warning(f"구글 id_token issuer 불일치: {payload.get('iss')}")
        raise BadRequestException("유효하지 않은 구글 id_token입니다.")

    return {
        "provider_id": str(payload.get("sub")),
        "email": payload.get("email"),
    }
