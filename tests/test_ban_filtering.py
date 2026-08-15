"""차단 필터링 자체 점검 — `python tests/test_ban_filtering.py`.

인메모리 SQLite에 user/reels/ban/like/comment 테이블을 세우고 DAO·서비스를 직접 돌린다.
프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.

지키려는 규칙: 차단한 사람의 릴스는 피드에서 빠지고 익명 릴스는 살아남는다,
내가 올린 릴스는 추천에서 빠진다, 그리고 **카드의 댓글 수 = 댓글 목록에 실제로
보이는 개수**(차단한 사람의 댓글과 그 댓글에 달린 답글까지 양쪽에서 똑같이 빠진다).
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from databases.daos import ban_dao, comment_dao, reels_dao
from databases.models.ban import Ban
from databases.models.base import Base
from databases.models.comment import Comment
from databases.models.like import Like
from databases.models.reels import Reels
from databases.models.user import User
from services import ban_service, comment_service, video_service


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

    # ── 카드의 댓글 수 = 내가 목록에서 실제로 보는 댓글 수 ────────────────────────
    theirs = db.query(Reels).filter(Reels.url == "https://x/other.mp4").one()
    mine = db.query(Reels).filter(Reels.url == "https://x/mine.mp4").one()

    def comment(reels, author, parent=None):
        c = Comment(reels_idx=reels.reels_idx, user_idx=author.user_idx,
                    content="c", parent_idx=parent.comment_idx if parent else None)
        db.add(c)
        db.flush()
        return c

    hidden_top = comment(theirs, blocked)          # 차단한 사람의 댓글 → 안 보임
    comment(theirs, other, hidden_top)             # 그 댓글의 답글 → 부모가 없어 같이 숨는다
    visible_top = comment(theirs, other)           # 보인다
    comment(theirs, blocked, visible_top)          # 차단한 사람의 답글 → 안 보임
    comment(theirs, me, visible_top)               # 내 답글 → 보인다(자기 자신은 차단이 아니다)
    comment(mine, blocked)                         # 내 릴스에 달린 차단한 사람의 댓글

    # DAO: 필터 없으면 전량, 차단 목록을 주면 부모까지 따져 뺀다
    assert comment_dao.counts_by_reels(db, [theirs.reels_idx]) == {theirs.reels_idx: 5}
    assert comment_dao.counts_by_reels(db, [theirs.reels_idx], [blocked.user_idx]) == {
        theirs.reels_idx: 2
    }

    # 불변식: 카드 숫자 == 댓글 목록에 실제로 담긴 개수(답글 포함)
    tree = comment_service.list_comments(db, me, theirs.reels_idx)
    visible = sum(1 + len(top.replies) for top in tree)
    card = next(r for r in video_service.recommend_reels(db, "", me) if r.reels_idx == theirs.reels_idx)
    assert card.comment_count == visible == 2, (card.comment_count, visible)

    # 마이페이지("내가 올린 릴스")도 같은 규칙 — 내 릴스에 달린 차단한 사람의 댓글은 안 센다
    my_card = next(
        i for i in video_service.list_my_reels(db, me, 10, None).items
        if i.reels_idx == mine.reels_idx
    )
    assert my_card.comment_count == 0, my_card.comment_count

    # 부모만 지워지고 답글은 살아남은 행도 안 센다. 보통은 soft_delete 가 답글까지 함께
    # 지우지만, '부모 삭제'와 '답글 작성'이 겹치면 cascade 의 UPDATE 가 아직 커밋 안 된
    # 답글을 못 봐 이 상태가 만들어진다 — 그 답글은 목록에서 부모를 잃어 안 보인다.
    orphan_parent = comment(theirs, other)
    comment(theirs, other, orphan_parent)      # 답글은 살려 둔다
    orphan_parent.deleted_at = datetime.now(timezone.utc)   # 부모만 지운다(캐스케이드 없이)
    db.flush()

    tree = comment_service.list_comments(db, me, theirs.reels_idx)
    visible = sum(1 + len(top.replies) for top in tree)
    counted = comment_dao.counts_by_reels(db, [theirs.reels_idx], [blocked.user_idx])
    assert counted[theirs.reels_idx] == visible == 2, (counted, visible)

    # 차단 목록에 뜬다(닉네임 + 프로필 사진) → 상대가 탈퇴하면 빠진다
    blocked.profile_image = "https://x/blocked.jpg"
    db.flush()
    assert ban_dao.list_blocked(db, me.user_idx) == [
        (blocked.user_idx, "blocked", "https://x/blocked.jpg")
    ]
    # 서비스가 튜플을 스키마에 옮길 때 닉네임·사진이 뒤바뀌지 않는지(순서 의존)
    [item] = ban_service.list_blocked(db, me)
    assert (item.nickname, item.profile_image) == ("blocked", "https://x/blocked.jpg"), item

    blocked.deleted_at = datetime.now(timezone.utc)
    db.flush()
    assert ban_dao.list_blocked(db, me.user_idx) == []

    print("ok")


if __name__ == "__main__":
    main()
