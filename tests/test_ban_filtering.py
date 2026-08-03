"""차단 필터링 자체 점검 — `python tests/test_ban_filtering.py`.

인메모리 SQLite에 user/reels/ban 3개 테이블만 세우고 DAO 두 개를 직접 돌린다.
프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from databases.daos import ban_dao, reels_dao
from databases.models.ban import Ban
from databases.models.base import Base
from databases.models.reels import Reels
from databases.models.user import User


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (User, Reels, Ban)])
    return sessionmaker(bind=engine)()


def _user(db, nickname):
    u = User(nickname=nickname, provider="google", provider_id=nickname)
    db.add(u)
    db.flush()
    return u


def main():
    db = _session()
    me, blocked, other = (_user(db, n) for n in ("me", "blocked", "other"))

    for owner in (blocked.user_idx, other.user_idx, None):  # None = 옛 익명 릴스
        db.add(Reels(user_idx=owner, url="https://x/v.mp4", title=None))
    db.add(Ban(user_idx=me.user_idx, blocked_user_idx=blocked.user_idx))
    db.flush()

    # 차단 전: 3개 다 보인다
    assert len(reels_dao.get_random_reels(db, 10, [], [])) == 3

    # 차단 후: 차단한 사람 것만 빠지고, user_idx NULL 인 익명 릴스는 살아남는다
    owners = {r.user_idx for r, *_ in reels_dao.get_random_reels(db, 10, [], [blocked.user_idx])}
    assert owners == {other.user_idx, None}, owners

    # 차단 목록에 뜬다 → 상대가 탈퇴하면 빠진다
    assert ban_dao.list_blocked(db, me.user_idx) == [(blocked.user_idx, "blocked")]
    blocked.deleted_at = datetime.now(timezone.utc)
    db.flush()
    assert ban_dao.list_blocked(db, me.user_idx) == []

    print("ok")


if __name__ == "__main__":
    main()
