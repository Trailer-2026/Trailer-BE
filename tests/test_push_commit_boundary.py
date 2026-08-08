"""푸시 발송의 커밋 경계 자체 점검 — `python tests/test_push_commit_boundary.py`.

프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.

**보는 것**: fcm_service.send_push 가 언제 커밋하는가. 죽은 토큰을 지웠을 때만 커밋해야
한다. 이 함수는 조회 요청 안에서도 불린다 — 풍경 알림은 이력을 남기지 않아
(push_service.notify record=False) GET /api/scenic-spots/nearby 의 요청 세션에서 그대로
돌기 때문이다. 남길 변경이 없는데 커밋하면 조회가 쓰기 트랜잭션을 여는 꼴이 된다.

Firebase 는 부르지 않는다 — utils.firebase.send_multicast 를 스텁으로 갈음한다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from databases.daos import fcm_token_dao
from services import fcm_service, push_service
from utils import firebase


class FakeSession:
    """commit/rollback 횟수만 세는 세션 대역."""

    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _Patch:
    """모듈 속성을 잠깐 갈아끼우고 되돌린다. targets = {(모듈, 속성명): 대체값}."""

    def __init__(self, targets):
        self.targets = targets
        self.saved = {}

    def __enter__(self):
        for key, value in self.targets.items():
            module, attr = key
            self.saved[key] = getattr(module, attr)
            setattr(module, attr, value)
        return self

    def __exit__(self, *exc):
        for (module, attr), value in self.saved.items():
            setattr(module, attr, value)


def _stub(tokens, dead):
    """토큰 조회·FCM 발송·토큰 삭제를 스텁으로. 삭제 호출 기록을 돌려준다."""
    deleted = []
    return deleted, _Patch({
        (fcm_token_dao, "get_tokens_by_user"): lambda db, user_idx: tokens,
        (fcm_token_dao, "soft_delete_by_tokens"): lambda db, t: deleted.append(list(t)),
        (firebase, "send_multicast"): lambda t, title, body, data=None: (
            len(t) - len(dead), len(dead), list(dead)
        ),
    })


def test_no_dead_tokens_no_commit():
    """정상 발송(죽은 토큰 없음) — 지울 게 없으니 커밋하지 않는다."""
    db = FakeSession()
    deleted, patch = _stub(tokens=["tok-a", "tok-b"], dead=[])
    with patch:
        result = fcm_service.send_push(db, 1, "제목", "본문")
    assert result.sent == 2 and result.failed == 0, result
    assert deleted == [], "지울 토큰이 없다"
    assert db.commits == 0, f"커밋하면 안 된다(실제 {db.commits}회)"
    print("  OK test_no_dead_tokens_no_commit")


def test_dead_tokens_commit_once():
    """죽은 토큰을 지웠으면 그 삭제를 커밋해야 한다 — 안 그러면 롤백돼 계속 재시도한다."""
    db = FakeSession()
    deleted, patch = _stub(tokens=["tok-a", "tok-dead"], dead=["tok-dead"])
    with patch:
        result = fcm_service.send_push(db, 1, "제목", "본문")
    assert result.sent == 1 and result.failed == 1, result
    assert deleted == [["tok-dead"]], deleted
    assert db.commits == 1, f"삭제는 커밋돼야 한다(실제 {db.commits}회)"
    print("  OK test_dead_tokens_commit_once")


def test_no_tokens_short_circuits():
    """등록된 기기가 없으면 FCM 도 커밋도 없다."""
    db = FakeSession()
    called = []
    deleted, patch = _stub(tokens=[], dead=[])
    with patch, _Patch({
        (firebase, "send_multicast"): lambda *a, **k: called.append(1) or (0, 0, [])
    }):
        result = fcm_service.send_push(db, 1, "제목", "본문")
    assert result.sent == 0 and result.failed == 0
    assert called == [], "토큰이 없으면 FCM 을 부르지 않는다"
    assert db.commits == 0, f"커밋하면 안 된다(실제 {db.commits}회)"
    print("  OK test_no_tokens_short_circuits")


def test_scenery_path_leaves_read_session_clean():
    """풍경 알림(record=False)은 조회 요청 세션을 건드리지 않는다.

    이력을 남기지 않으므로 notify 안에서 커밋할 일이 없고, send_push 도 지운 토큰이
    없으면 커밋하지 않는다 → 조회 요청은 끝까지 읽기로 남는다.
    """
    db = FakeSession()

    class FakeUser:
        user_idx, nickname = 1, "tester"

    deleted, patch = _stub(tokens=["tok-a"], dead=[])
    with patch, _Patch({
        # 수신 설정 조회는 DB 를 타므로 '설정 행 없음'(=기본 수신)으로 갈음한다.
        (push_service.notification_dao, "get_by_user"): lambda db_, idx: None,
    }):
        sent = push_service.notify_scenery(
            db, FakeUser(), [{"scenic_spot_idx": 7}], "대전역"
        )
    assert sent is True, "보이는 관광지가 있으면 발송한다"
    assert db.commits == 0, f"조회 요청이 커밋했다(실제 {db.commits}회)"
    assert db.rollbacks == 0, f"예외 없이 끝나야 한다(롤백 {db.rollbacks}회)"
    print("  OK test_scenery_path_leaves_read_session_clean")


def test_scenery_skips_when_nothing_visible():
    """보이는 곳이 없으면 빈 알림을 보내지 않는다."""
    db = FakeSession()

    class FakeUser:
        user_idx, nickname = 1, "tester"

    assert push_service.notify_scenery(db, FakeUser(), [], "대전역") is False
    assert db.commits == 0
    print("  OK test_scenery_skips_when_nothing_visible")


def main():
    test_no_dead_tokens_no_commit()
    test_dead_tokens_commit_once()
    test_no_tokens_short_circuits()
    test_scenery_path_leaves_read_session_clean()
    test_scenery_skips_when_nothing_visible()
    print("푸시 커밋 경계 selfcheck OK")


if __name__ == "__main__":
    main()
