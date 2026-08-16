"""릴스 영상 주소 조회(`GET /api/videos/reels/{reels_idx}/url`) 자체 점검 — `python tests/test_reels_url.py`.

딥링크가 URL 대신 reels_idx 만 들고 다니게 하는 API 라, 되찾은 주소가 그 PK 의 것이
맞는지와 편집 화면을 열지 말아야 할 경우(is_mine=False)를 확인한다.
프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

from core.exceptions.custom import NotFoundException
from databases.models.base import Base
from databases.models.reels import Reels
from databases.models.user import User
from services import video_service


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (User, Reels)])
    return sessionmaker(bind=engine)()


def _reels(db, user_idx, url, title=None, deleted=False):
    reels = Reels(user_idx=user_idx, url=url, title=title)
    if deleted:
        reels.deleted_at = func.now()
    db.add(reels)
    db.flush()
    return reels


def main():
    db = _session()
    me = User(nickname="나", provider="google", provider_id="me")
    other = User(nickname="남", provider="google", provider_id="other")
    db.add_all([me, other])
    db.flush()

    mine = _reels(db, me.user_idx, "https://bucket/mine.mp4", "부산 2박 3일")
    theirs = _reels(db, other.user_idx, "https://bucket/theirs.mp4")
    rendering = _reels(db, me.user_idx, "")  # 렌더 미완료 자리표
    gone = _reels(db, me.user_idx, "https://bucket/gone.mp4", deleted=True)

    # 1) 내 릴스 — PK 에 대응하는 주소가 그대로, is_mine=True.
    got = video_service.get_reels_url(db, mine.reels_idx, me.user_idx)
    assert got.url == "https://bucket/mine.mp4", got.url
    assert got.reels_idx == mine.reels_idx
    assert got.title == "부산 2박 3일", got.title
    assert got.is_mine is True, "내 릴스는 is_mine 이 True 여야 한다"

    # 2) 남의 릴스 — 공개 피드라 주소는 나가되 편집 화면을 열면 안 되므로 is_mine=False.
    got = video_service.get_reels_url(db, theirs.reels_idx, me.user_idx)
    assert got.url == "https://bucket/theirs.mp4", got.url
    assert got.is_mine is False, "남의 릴스는 is_mine 이 False 여야 한다"

    # 3) 렌더 미완료·삭제·없는 PK 는 전부 404 — 딥링크가 죽은 화면을 열지 않게.
    for idx, why in (
        (rendering.reels_idx, "렌더 미완료"),
        (gone.reels_idx, "삭제된 릴스"),
        (999999, "없는 릴스"),
    ):
        try:
            video_service.get_reels_url(db, idx, me.user_idx)
        except NotFoundException:
            pass
        else:
            raise AssertionError(f"{why} 는 404 여야 한다")

    print("OK: 릴스 영상 주소 조회 셀프체크 통과")


if __name__ == "__main__":
    main()
