"""렌더 미완료 릴스 취급 자체 점검 — `python tests/test_reels_ready.py`.

인메모리 SQLite 에 user/reels 두 테이블만 세운다.
프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.

**보는 것**: url 이 빈 문자열인 자리표 행(렌더 시작 때 reels_idx 를 발급하려고 미리 만든
행)은 아직 사용자 컨텐츠가 아니다. 피드·마이페이지가 이미 그 조건으로 거르고 있으니
좋아요·댓글도 같은 규칙을 따라야 한다 — 자리표에 FK 가 걸리면 렌더 실패 시 정리
(video_service._discard_pending_reels)가 하드 삭제를 못 하고 소프트 삭제로 물러선다.

반대로 렌더 진행률·다운로드는 그 행 자체를 봐야 하므로 get_by_idx 가 그대로 남아야 한다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from core.exceptions.custom import NotFoundException
from databases.daos import reels_dao
from databases.models.ban import Ban
from databases.models.base import Base
from databases.models.comment import Comment
from databases.models.like import Like
from databases.models.reels import Reels
from databases.models.user import User
from services import comment_service, like_service, video_service

# 좋아요·댓글 경로가 실제로 타는 테이블 전부(차단 필터가 ban 을 읽는다).
_TABLES = (User, Reels, Like, Comment, Ban)


class FakeUser:
    user_idx, nickname, profile_image = 1, "tester", None


def _seeded():
    """(세션, 완성 릴스, 렌더중 릴스, 삭제된 릴스)."""
    engine = create_engine("sqlite:///:memory:")

    # likes 의 CHECK 는 Postgres 함수 num_nonnulls 를 쓴다(좋아요 대상은 릴스/댓글 중 하나).
    # 제약을 떼는 대신 같은 함수를 SQLite 에 등록해 제약이 살아 있는 채로 돌린다.
    @event.listens_for(engine, "connect")
    def _register_num_nonnulls(dbapi_conn, _record):
        dbapi_conn.create_function(
            "num_nonnulls", -1, lambda *args: sum(a is not None for a in args)
        )

    Base.metadata.create_all(engine, tables=[m.__table__ for m in _TABLES])
    db = sessionmaker(bind=engine)()

    # 댓글 목록은 작성자를 조인해 닉네임을 붙인다 — 행이 없으면 댓글이 통째로 빠진다.
    db.add(User(
        user_idx=FakeUser.user_idx, nickname=FakeUser.nickname,
        provider="google", provider_id="sub-1",
    ))
    db.flush()

    ready = reels_dao.create(db, user_idx=None, url="https://x/done.mp4", title="완성")
    pending = reels_dao.create(
        db, user_idx=None, url=video_service.PENDING_REELS_URL, title="렌더중"
    )
    removed = reels_dao.create(db, user_idx=None, url="https://x/gone.mp4", title="삭제됨")
    reels_dao.soft_delete(db, removed)
    db.commit()
    return db, ready, pending, removed


def test_pending_url_is_empty_string():
    """자리표는 NULL 이 아니라 빈 문자열이다(url 은 NOT NULL)."""
    assert video_service.PENDING_REELS_URL == "", repr(video_service.PENDING_REELS_URL)
    print("  OK test_pending_url_is_empty_string")


def test_get_ready_filters_pending_and_deleted():
    db, ready, pending, removed = _seeded()
    assert reels_dao.get_ready_by_idx(db, ready.reels_idx) is not None
    assert reels_dao.get_ready_by_idx(db, pending.reels_idx) is None, "렌더중은 걸러야 한다"
    assert reels_dao.get_ready_by_idx(db, removed.reels_idx) is None, "삭제된 것도 걸러야 한다"
    print("  OK test_get_ready_filters_pending_and_deleted")


def test_render_path_still_sees_placeholder():
    """진행률 조회는 자리표 행을 봐야 한다 — 여기까지 걸러 버리면 404 가 나간다."""
    db, ready, pending, removed = _seeded()
    assert reels_dao.get_by_idx(db, pending.reels_idx) is not None, (
        "렌더 경로가 자리표를 못 보면 진행률 대신 404 가 나간다"
    )
    assert reels_dao.get_by_idx(db, removed.reels_idx) is None, "소프트 삭제는 여전히 제외"
    print("  OK test_render_path_still_sees_placeholder")


def test_like_and_comment_reject_pending():
    """렌더 미완료 릴스에는 좋아요·댓글이 붙지 않는다(404)."""
    db, ready, pending, removed = _seeded()
    user = FakeUser()

    blocked = {
        "like_reels": lambda: like_service.like_reels(db, user, pending.reels_idx),
        "unlike_reels": lambda: like_service.unlike_reels(db, user, pending.reels_idx),
        "create_comment": lambda: comment_service.create_comment(
            db, user, pending.reels_idx, "hi", None
        ),
        "list_comments": lambda: comment_service.list_comments(db, user, pending.reels_idx),
    }
    for name, call in blocked.items():
        try:
            call()
        except NotFoundException:
            continue
        raise AssertionError(f"{name}: 렌더 미완료 릴스를 통과시켰다")
    print("  OK test_like_and_comment_reject_pending")


def test_completed_reels_still_work():
    """완성된 릴스는 그대로 동작한다 — 차단이 과하게 걸리지 않았는지."""
    db, ready, pending, removed = _seeded()
    user = FakeUser()

    result = like_service.like_reels(db, user, ready.reels_idx)
    assert result.liked is True and result.like_count == 1, result
    # 멱등 — 두 번 눌러도 개수가 늘지 않는다.
    assert like_service.like_reels(db, user, ready.reels_idx).like_count == 1

    comment = comment_service.create_comment(db, user, ready.reels_idx, "좋아요", None)
    assert comment.content == "좋아요"
    assert len(comment_service.list_comments(db, user, ready.reels_idx)) == 1
    print("  OK test_completed_reels_still_work")


def main():
    test_pending_url_is_empty_string()
    test_get_ready_filters_pending_and_deleted()
    test_render_path_still_sees_placeholder()
    test_like_and_comment_reject_pending()
    test_completed_reels_still_work()
    print("렌더 미완료 릴스 selfcheck OK")


if __name__ == "__main__":
    main()
