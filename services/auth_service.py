import logging
import secrets

from sqlalchemy.orm import Session

from databases.daos import user_dao, refresh_token_dao, fcm_token_dao
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

# ── Play 스토어 심사용 데모 계정 ────────────────────────────────────────────
# 심사가 끝나면 **이 블록과 demo_login·라우터를 지우고 배포**해야 닫힌다.
# 자격증명을 코드에 둔 선택이라 설정 삭제만으로는 닫히지 않는다(레포가 공개라
# 이 값은 공개 정보로 취급한다 — 아래 demo_login docstring 참고).
DEMO_USERNAME = "trailer"
DEMO_PASSWORD = "trailer2026!"

# 계정의 (provider, provider_id). provider 가 'demo' 라 소셜 계정과 유니크 슬롯이
# 겹치지 않는다 — 심사가 끝나도 이 행은 권한 없는 평범한 유저로 남을 뿐이다.
DEMO_PROVIDER = "demo"
DEMO_PROVIDER_ID = "store-review"
DEMO_NICKNAME = "트레일러 데모"


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


def demo_login(username: str, password: str, db: Session) -> TokenResponse:
    """Play 스토어 심사용 로그인 — 소셜 제공자를 거치지 않고 자체 JWT를 발급한다.

    **왜 필요한가**: 이 앱은 소셜 로그인뿐인데, 인증의 마지막 관문이 우리 서버가 아니라
    구글/카카오 쪽에 있어 심사원 손에 들어가지 않는다. 해외 IP에서 접속하는 심사원은
    구글은 위험 판정으로 막히고, 카카오는 확인 메시지가 계정 주인 폰으로 가 누를 수가 없다.
    Play Console '앱 액세스'에 데모 자격증명을 넣는 것은 정식 허용 방식이다.

    **자격증명이 코드에 있다**(위 상수). 레포가 공개라 이 값은 공개 정보이고, 아무나 이
    계정으로 로그인할 수 있다는 뜻이다 — 권한 없는 일반 사용자 하나지만 릴스·댓글은
    홈 피드에 노출되므로 스팸이 들어올 수 있다. 그래서 **심사가 끝나면 반드시 지운다**:
    이 함수와 라우터(`routers/auth.py` POST /api/auth/login/demo), 스키마
    (`DemoLoginRequest`)를 지우고 main 에 푸시하면 닫힌다. 설정 파일을 지우는 것으로는
    닫히지 않는다. 그 사이 계정이 더럽혀지면 그 유저를 탈퇴 처리하면 새로 만들어진다.

    발급되는 계정은 권한 없는 평범한 일반 사용자다(가입 흐름을 소셜과 그대로 공유한다).
    """
    # compare_digest 는 str 이면 ASCII 만 받으므로(비ASCII 는 TypeError) bytes 로 비교한다.
    # 둘 다 평가해 먼저 틀린 쪽으로 응답 시간이 갈리지 않게 한다.
    ok_username = secrets.compare_digest(username.encode(), DEMO_USERNAME.encode())
    ok_password = secrets.compare_digest(password.encode(), DEMO_PASSWORD.encode())
    if not (ok_username and ok_password):
        raise UnauthorizedException("아이디 또는 비밀번호가 올바르지 않습니다.")

    logger.info("데모 로그인(심사용) 발급")
    return _login_with_social_user(
        DEMO_PROVIDER, {"provider_id": DEMO_PROVIDER_ID, "email": None}, db
    )


def ensure_demo_user(db: Session) -> User:
    """데모 계정 행을 만들어 둔다(없을 때만). 서버 기동 시 1회 — databases/provision.py.

    로그인이 get-or-create 라 미리 없어도 그 자리에서 만들어지지만, **심사원이 보기 전에
    계정이 있어야 여행·릴스 같은 볼거리를 붙여 둘 수 있다**. 빈 계정으로 심사받으면
    첫 화면이 텅 빈 채로 시작한다.

    닉네임만 고정으로 준다 — 운영 DB에서 이 계정을 눈으로 찾을 때 랜덤 닉네임이면
    매번 user_idx 를 뒤져야 한다. 그 외에는 권한 없는 평범한 일반 사용자다.
    """
    user = user_dao.get_by_provider(db, DEMO_PROVIDER, DEMO_PROVIDER_ID)
    if user:
        return user

    user = user_dao.create(db, DEMO_PROVIDER, DEMO_PROVIDER_ID, None, nickname=DEMO_NICKNAME)
    db.commit()
    logger.info("데모 계정 생성 (user_idx=%s)", user.user_idx)
    return user


async def social_login(provider: str, access_token: str, db: Session) -> TokenResponse:
    # access_token 방식은 현재 kakao만. 새 provider가 같은 흐름을 공유하면 그때 분기 추가.
    if provider != "kakao":
        raise BadRequestException("지원하지 않는 소셜 제공자입니다.")

    social_user = await oauth.fetch_kakao_user(access_token)
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
    """전달한 refresh 토큰을 무효화하고 그 사용자의 기기 토큰을 모두 해제한다.

    이미 만료/무효인 토큰이면 조용히 성공 처리(멱등).

    **왜 그 기기 하나가 아니라 전부인가**: 로그아웃한 기기로 푸시가 계속 가면 안 되는데
    (본문에 여행 제목·닉네임·좌석번호가 실려, 기기를 넘기거나 공유하면 다음 사람이 이전
    사용자의 알림을 읽는다), 서버는 **어느 기기가 로그아웃하는지 알 수 없다** — access
    토큰에 세션 식별자가 없고(core.security._create_token) refresh 토큰의 jti는 기기가
    아니라 발급 회차라 회전 때마다 바뀐다. 앱이 기기 토큰을 같이 보내주면 정확히 하나만
    끊을 수 있지만 그건 요청 스키마를 바꾸는 일이라, 우선 안전한 쪽(과하게 지우기)으로 뒀다.

    대가는 다중 기기 사용자가 한 기기에서 로그아웃하면 **다른 기기도 앱을 다시 열어
    토큰을 재등록할 때까지 푸시가 멈춘다**는 것. 놓친 알림은 알림 화면 이력에 남으므로
    사라지지는 않는다. 이게 거슬리면 로그아웃 요청에 fcm_token을 선택 항목으로 받고
    fcm_token_dao 에 소유자까지 함께 거는 삭제를 붙이면 된다(소유를 안 보면 토큰
    문자열만으로 남의 기기 푸시를 끊는 수단이 된다 — 이 엔드포인트엔 access 인증이 없다).
    """
    try:
        payload = decode_token(token, expected_type="refresh")
    except UnauthorizedException:
        return  # 이미 못 쓰는 토큰 → 로그아웃은 성공으로 간주(멱등)

    # 기기 토큰 해제는 **살아 있던 세션을 실제로 끊었을 때만** 한다(revoke_by_jti는 아직
    # 무효화되지 않은 행만 세므로 0이면 이미 로그아웃된 토큰의 재사용이다). 서명·만료만
    # 보고 지우면, 폐기됐지만 아직 만료 전인 refresh 토큰(로그·백업에 남은 것)을 주워
    # 반복 호출하는 것만으로 그 사용자의 푸시를 계속 꺼 버릴 수 있다.
    jti = payload.get("jti")
    if jti and refresh_token_dao.revoke_by_jti(db, jti):
        fcm_token_dao.soft_delete_by_user(db, int(payload["sub"]))
    db.commit()


def logout_all(user_idx: int, db: Session) -> None:
    """해당 사용자의 모든 refresh 토큰과 기기 토큰을 무효화한다 (모든 기기 로그아웃).

    기기 토큰까지 지우는 이유: '모든 기기에서 로그아웃'은 기기를 잃어버렸을 때 쓰는
    기능인데, 세션만 끊고 푸시를 그대로 두면 남의 손에 있는 그 기기로 알림이 계속 간다.
    """
    refresh_token_dao.revoke_all_for_user(db, user_idx)
    fcm_token_dao.soft_delete_by_user(db, user_idx)
    db.commit()


def withdraw(user_idx: int, db: Session) -> None:
    """회원 탈퇴 — 모든 refresh 토큰 폐기 + FCM 토큰 정리 + 유저 소프트 삭제.

    soft-delete라 user_idx를 FK로 물린 다른 테이블은 FK가 깨지진 않지만 데이터가 남는다.
    유저 소유 테이블을 새로 추가하면 여기서 함께 soft-delete하도록 한 줄씩 보태야 한다.
    (예: travel/schedule 머지 시 travel_dao.soft_delete_by_user 등 추가)
    현재 정리 대상: refresh_token, fcm_token, user.
    """
    refresh_token_dao.revoke_all_for_user(db, user_idx)
    fcm_token_dao.soft_delete_by_user(db, user_idx)
    user_dao.soft_delete(db, user_idx)
    db.commit()
