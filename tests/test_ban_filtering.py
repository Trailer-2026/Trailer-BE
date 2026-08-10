"""차단 필터링 자체 점검 — `python tests/test_ban_filtering.py`.

인메모리 SQLite에 user/reels/ban 3개 테이블만 세우고 DAO 두 개를 직접 돌린다.
프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from databases.daos import ban_dao, reels_dao
from databases.models.ban import Ban
from databases.models.base import Base
from databases.models.comment import Comment
from databases.models.like import Like
from databases.models.reels import Reels
from databases.models.user import User
from services import video_service


def _session():
    engine = create_engine("sqlite:///:memory:")

    # likes 의 CHECK 는 Postgres 함수(num_nonnulls)라 SQLite 가 CREATE TABLE 부터
    # 거부한다 — 같은 뜻의 함수를 붙여 실제 DAO 를 그대로 돌린다.
    @event.listens_for(engine, "connect")
    def _add_num_nonnulls(dbapi_conn, _record):
        dbapi_conn.create_function(
            "num_nonnulls", 2, lambda *args: sum(a is not None for a in args)
        )

    Base.metadata.create_all(
        engine, tables=[t.__table__ for t in (User, Reels, Ban, Like, Comment)]
    )
    return sessionmaker(bind=engine)()


def _user(db, nickname):
    u = User(nickname=nickname, provider="google", provider_id=nickname)
    db.add(u)
    db.flush()
    return u


def main():
    db = _session()
    me, blocked, other = (_user(db, n) for n in ("me", "blocked", "other"))

    # 작성자별로 url 을 다르게 준다 — 피드 결과를 url 집합으로 확인하므로 같은 값이면
    # 차단한 사람 것이 섞여 들어와도 어셔션이 통과해 버린다.
    for owner, url in (
        (blocked.user_idx, "https://x/blocked.mp4"),
        (other.user_idx, "https://x/other.mp4"),
        (None, "https://x/anon.mp4"),  # None = 옛 익명 릴스
    ):
        db.add(Reels(user_idx=owner, url=url, title=None))
    db.add(Ban(user_idx=me.user_idx, blocked_user_idx=blocked.user_idx))
    db.flush()

    # 차단 전: 3개 다 보인다
    assert len(reels_dao.get_random_reels(db, 10, [], [])) == 3

    # 차단 후: 차단한 사람 것만 빠지고, user_idx NULL 인 익명 릴스는 살아남는다
    owners = {r.user_idx for r, *_ in reels_dao.get_random_reels(db, 10, [], [blocked.user_idx])}
    assert owners == {other.user_idx, None}, owners

    # 피드 합성 단계: 내가 올린 릴스는 추천에서 빠진다 (내 건 마이페이지에서 본다)
    db.add(Reels(user_idx=me.user_idx, url="https://x/mine.mp4", title=None))
    db.flush()
    urls = {r.url for r in video_service.recommend_reels(db, "", me)}
    assert urls == {"https://x/other.mp4", "https://x/anon.mp4"}, urls
    # 비로그인은 '내 것'이 없으니 전부 보인다
    assert len(video_service.recommend_reels(db, "", None)) == 4

    # 차단 목록에 뜬다 → 상대가 탈퇴하면 빠진다
    assert ban_dao.list_blocked(db, me.user_idx) == [(blocked.user_idx, "blocked")]
    blocked.deleted_at = datetime.now(timezone.utc)
    db.flush()
    assert ban_dao.list_blocked(db, me.user_idx) == []

    print("ok")


if __name__ == "__main__":
    main()
