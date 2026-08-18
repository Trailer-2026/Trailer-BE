"""FCM 기기 토큰 수명 자체 점검 — `python tests/test_fcm_token_lifecycle.py`.

프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.
인메모리 SQLite 라 네트워크·운영 DB 없이 돈다.

지키려는 규칙: **로그아웃한 기기로는 푸시가 가지 않는다**(어느 기기인지 알 수 없어
그 사용자 것을 전부 해제한다), 남의 토큰은 건드리지 않는다, 모든 기기 로그아웃·탈퇴도
같이 정리한다, 재로그인하면 등록이 되살아난다, 그리고 인증 없이 임의 사용자에게 쏘던
/api/fcm/test-send 는 등록돼 있지 않다.
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
# 토큰 수명이지 서명 설정이 아니므로 로컬 설정 파일에 기대지 않는다.
from core import security
security.SECRET_KEY = "selfcheck-secret"

from databases.models.base import Base
from databases.models.fcm_token import FcmToken
from databases.models.refresh_token import RefreshToken
from databases.models.user import User
from services import auth_service, fcm_service
from databases.daos import fcm_token_dao

USER = 1
OTHER = 2


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (User, FcmToken, RefreshToken)])
    db = sessionmaker(bind=engine)()
    for idx in (USER, OTHER):
        db.add(User(user_idx=idx, nickname=f"u{idx}", provider="google", provider_id=f"p{idx}"))
    db.commit()
    return db


def _refresh_token_of(db, user_idx: int) -> str:
    """로그인해서 refresh 토큰을 받는다 — 로그아웃이 소유자를 이걸로 판정한다."""
    user = db.query(User).filter(User.user_idx == user_idx).one()
    return auth_service._issue_tokens(user, db).refresh_token


def test_logout_unregisters_devices() -> None:
    """어느 기기가 로그아웃하는지 서버가 알 수 없어(요청에 기기 정보가 없다) 전부 해제한다."""
    db = _session()
    fcm_service.register_token(db, USER, "phone")
    fcm_service.register_token(db, USER, "tablet")
    fcm_service.register_token(db, OTHER, "other-phone")

    auth_service.logout(_refresh_token_of(db, USER), db)

    left = fcm_token_dao.get_tokens_by_user(db, USER)
    assert left == [], f"로그아웃 후에도 기기 토큰이 남았다: {left}"
    assert fcm_token_dao.get_tokens_by_user(db, OTHER) == ["other-phone"], "남의 토큰까지 지워졌다"
    print("OK: 로그아웃하면 그 사용자 기기로는 푸시가 안 간다")


def test_logout_with_dead_token_is_noop() -> None:
    """못 쓰는 refresh 토큰이면 아무것도 안 지운다 — 조용히 성공(멱등)만 한다.

    특히 **이미 폐기된(그러나 아직 만료 전인) 토큰의 재사용**이 중요하다. 서명·만료만 보고
    지우면, 로그·백업에 남은 옛 토큰을 주워 반복 호출하는 것만으로 그 사용자의 푸시를
    계속 꺼 버릴 수 있다.
    """
    db = _session()
    fcm_service.register_token(db, USER, "phone")

    auth_service.logout("not-a-jwt", db)          # 파싱 자체가 안 되는 토큰
    assert fcm_token_dao.get_tokens_by_user(db, USER) == ["phone"], \
        "무효 토큰으로도 기기 토큰이 지워졌다"

    revoked = _refresh_token_of(db, USER)
    auth_service.logout(revoked, db)              # 정상 로그아웃 → 여기서 해제된다
    assert fcm_token_dao.get_tokens_by_user(db, USER) == []

    fcm_service.register_token(db, USER, "phone")  # 다시 로그인한 기기
    auth_service.logout(revoked, db)              # 폐기된 토큰 재사용
    assert fcm_token_dao.get_tokens_by_user(db, USER) == ["phone"], \
        "폐기된 refresh 토큰 재사용으로 기기 토큰이 지워졌다(푸시 DoS)"
    print("OK: 무효·폐기된 refresh 토큰 로그아웃은 아무것도 안 지운다")


def test_logout_all_and_withdraw_clear_devices() -> None:
    db = _session()
    fcm_service.register_token(db, USER, "phone")
    fcm_service.register_token(db, OTHER, "other-phone")

    auth_service.logout_all(USER, db)
    assert fcm_token_dao.get_tokens_by_user(db, USER) == [], "모든 기기 로그아웃 후에도 토큰이 남았다"
    assert fcm_token_dao.get_tokens_by_user(db, OTHER) == ["other-phone"], "남의 토큰까지 지워졌다"

    auth_service.withdraw(OTHER, db)
    assert fcm_token_dao.get_tokens_by_user(db, OTHER) == [], "탈퇴 후에도 토큰이 남았다"
    print("OK: 모든 기기 로그아웃·탈퇴가 기기 토큰까지 정리한다")


def test_relogin_on_same_device_revives_token() -> None:
    """로그아웃한 기기에 다시 로그인하면 등록이 되살아난다(token UNIQUE 라 INSERT 는 못 한다)."""
    db = _session()
    fcm_service.register_token(db, USER, "phone")
    auth_service.logout(_refresh_token_of(db, USER), db)

    fcm_service.register_token(db, OTHER, "phone")  # 기기를 넘겨받은 다른 사람

    assert fcm_token_dao.get_tokens_by_user(db, OTHER) == ["phone"], "재등록이 안 됐다"
    assert fcm_token_dao.get_tokens_by_user(db, USER) == [], "이전 사용자에게 토큰이 남았다"
    print("OK: 로그아웃한 기기의 재등록은 그대로 된다")


def test_dev_test_send_route_removed() -> None:
    from main import app

    paths = {route.path for route in app.routes if route.path.startswith("/api/fcm")}
    assert "/api/fcm/test-send" not in paths, f"인증 없는 테스트 발송 API가 등록됨: {sorted(paths)}"
    print("OK: /api/fcm/test-send 미등록")


if __name__ == "__main__":
    test_logout_unregisters_devices()
    test_logout_with_dead_token_is_noop()
    test_logout_all_and_withdraw_clear_devices()
    test_relogin_on_same_device_revives_token()
    test_dev_test_send_route_removed()
