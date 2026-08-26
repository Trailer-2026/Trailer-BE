"""데모 로그인(스토어 심사용) 자체 점검 — `python tests/test_demo_login.py`.

프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.
인메모리 SQLite 라 네트워크·운영 DB 없이 돈다.

지키려는 규칙: 아이디·비밀번호가 정확히 맞을 때만 통과한다(빈 값·부분 일치는 401),
맞으면 소셜 로그인과 같은 토큰이 나오고, 계정은 provider='demo' 하나뿐이며 소셜 계정과
유니크 슬롯이 겹치지 않고, 여러 번 로그인해도 그 하나를 재사용한다.

※ 자격증명은 코드 상수(auth_service.DEMO_USERNAME/DEMO_PASSWORD)라, 이 점검은 값 자체가
  아니라 **상수와 대조하는 동작**을 본다. 심사가 끝나 엔드포인트를 지우면 이 파일도 함께
  지운다(임포트가 깨져 바로 드러난다).
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("OPENAPI_EXPORT", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# JWT 서명 키를 여기서 박는다 — config/properties_dev.ini 는 gitignore 라 CI 에 없고,
# 그러면 SECRET_KEY 가 None 이라 jwt.encode 가 TypeError 로 죽는다. 이 점검이 보려는 건
# 자격증명 대조와 토큰 발급이지 서명 설정이 아니므로 로컬 설정 파일에 기대지 않는다.
from core import security
security.SECRET_KEY = "selfcheck-secret"

from core.exceptions.custom import UnauthorizedException
from databases.daos import refresh_token_dao
from databases.models.base import Base
from databases.models.refresh_token import RefreshToken
from databases.models.user import User
from services import auth_service

USERNAME = auth_service.DEMO_USERNAME
PASSWORD = auth_service.DEMO_PASSWORD


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (User, RefreshToken)])
    return sessionmaker(bind=engine)()


def _expect_401(db, username: str, password: str, why: str) -> None:
    try:
        auth_service.demo_login(username, password, db)
    except UnauthorizedException:
        return
    raise AssertionError(why)


def _demo_users(db):
    return db.query(User).filter(User.provider == auth_service.DEMO_PROVIDER).all()


def test_wrong_credentials_rejected() -> None:
    """정확히 일치할 때만 통과한다.

    빈 값을 따로 보는 이유: compare_digest 는 빈 문자열끼리 True 라, 상수가 실수로 비어
    있으면 **아무나 빈 값으로 들어온다**. 상수가 비는 순간 여기서 걸리게 둔다.
    """
    db = _session()

    _expect_401(db, USERNAME, PASSWORD + "x", "틀린 비밀번호로 로그인됐다")
    _expect_401(db, "someone-else", PASSWORD, "틀린 아이디로 로그인됐다")
    _expect_401(db, USERNAME, "", "빈 비밀번호로 로그인됐다")
    _expect_401(db, "", "", "빈 값으로 로그인됐다")
    _expect_401(db, USERNAME.upper() + "!", PASSWORD, "아이디가 부분만 맞는데 로그인됐다")

    assert _demo_users(db) == [], "로그인 실패인데 계정이 만들어졌다"
    print("OK: 아이디·비밀번호가 틀리면 401 (빈 값 포함)")


def test_valid_credentials_issue_tokens() -> None:
    db = _session()

    tokens = auth_service.demo_login(USERNAME, PASSWORD, db)

    assert tokens.token_type == "bearer"
    users = _demo_users(db)
    assert len(users) == 1, f"데모 계정이 1개가 아니다: {users}"
    user = users[0]
    assert user.provider_id == auth_service.DEMO_PROVIDER_ID
    assert user.nickname, "닉네임이 비어 있다(소셜 가입과 같은 흐름이어야 한다)"

    # 소셜 로그인과 같은 토큰이어야 한다 — access 는 그대로 인증에 쓰이고,
    # refresh 는 화이트리스트에 올라가 있어야 재발급이 된다.
    access = security.decode_token(tokens.access_token, expected_type="access")
    assert int(access["sub"]) == user.user_idx
    refresh = security.decode_token(tokens.refresh_token, expected_type="refresh")
    assert int(refresh["sub"]) == user.user_idx
    assert refresh_token_dao.get_active_by_jti(db, refresh["jti"]) is not None, \
        "refresh 토큰이 화이트리스트에 없다"

    print("OK: 올바른 자격증명이면 소셜 로그인과 같은 토큰이 나온다")


def test_demo_account_does_not_collide_with_social() -> None:
    """provider 가 'demo' 라 (provider, provider_id) 유니크가 소셜 계정과 안 겹친다.

    같은 provider_id 를 쓰는 구글 사용자가 있어도 서로 다른 계정이어야 한다.
    """
    db = _session()
    db.add(User(nickname="g", provider="google", provider_id=auth_service.DEMO_PROVIDER_ID))
    db.commit()

    auth_service.demo_login(USERNAME, PASSWORD, db)

    demo = _demo_users(db)
    assert len(demo) == 1, f"데모 계정이 1개가 아니다: {demo}"
    assert demo[0].provider_id == auth_service.DEMO_PROVIDER_ID
    assert db.query(User).count() == 2, "소셜 계정과 뒤섞였다"
    print("OK: 데모 계정은 소셜 계정과 유니크 슬롯이 겹치지 않는다")


def test_seeded_user_is_reused_by_login() -> None:
    """서버 기동 시 미리 만들어 둔 계정을 로그인이 그대로 쓴다(계정이 둘로 갈리면 안 된다).

    미리 만드는 이유는 심사원이 볼 여행·릴스를 붙여 두기 위해서다 — 로그인이 새 계정을
    만들어 버리면 붙여 둔 데이터가 안 보인다.
    """
    db = _session()

    seeded = auth_service.ensure_demo_user(db)
    assert seeded.nickname == auth_service.DEMO_NICKNAME
    again = auth_service.ensure_demo_user(db)
    assert again.user_idx == seeded.user_idx, "기동할 때마다 계정이 새로 생긴다"

    auth_service.demo_login(USERNAME, PASSWORD, db)

    users = _demo_users(db)
    assert len(users) == 1, f"미리 만든 계정과 별개로 하나 더 생겼다: {users}"
    assert users[0].user_idx == seeded.user_idx
    assert users[0].nickname == auth_service.DEMO_NICKNAME, "로그인이 닉네임을 덮어썼다"
    print("OK: 미리 만들어 둔 데모 계정을 로그인이 그대로 쓴다")


def test_repeated_login_reuses_account() -> None:
    """심사원이 몇 번을 로그인해도 계정은 하나다."""
    db = _session()

    first = auth_service.demo_login(USERNAME, PASSWORD, db)
    second = auth_service.demo_login(USERNAME, PASSWORD, db)

    users = _demo_users(db)
    assert len(users) == 1, f"로그인할 때마다 계정이 생긴다: {users}"
    assert first.refresh_token != second.refresh_token, "매번 새 refresh 토큰이어야 한다"
    print("OK: 여러 번 로그인해도 데모 계정은 하나")


def test_demo_route_registered() -> None:
    from main import app

    paths = {route.path for route in app.routes if route.path.startswith("/api/auth")}
    assert "/api/auth/login/demo" in paths, f"엔드포인트가 등록되지 않았다: {sorted(paths)}"
    print("OK: POST /api/auth/login/demo 등록됨")


if __name__ == "__main__":
    test_wrong_credentials_rejected()
    test_valid_credentials_issue_tokens()
    test_demo_account_does_not_collide_with_social()
    test_seeded_user_is_reused_by_login()
    test_repeated_login_reuses_account()
    test_demo_route_registered()
