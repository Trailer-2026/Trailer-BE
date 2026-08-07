from sqlalchemy import func
from sqlalchemy.orm import Session

from databases.models.like import Like
from databases.models.reels import Reels
from databases.models.user import User


def get_by_idx(db: Session, reels_idx: int) -> Reels | None:
    """reels_idx로 단건 조회 (soft-delete 제외)."""
    return db.query(Reels).filter(
        Reels.reels_idx == reels_idx,
        Reels.deleted_at.is_(None),
    ).first()


def create(
    db: Session, *, user_idx: int | None, url: str, title: str | None,
    region: str | None = None,
) -> Reels:
    """릴스 행 생성 (flush만 — commit은 서비스가)."""
    reels = Reels(user_idx=user_idx, url=url, title=title, region=region)
    db.add(reels)
    db.flush()
    return reels


def update_url(db: Session, reels: Reels, url: str, thumbnail_url: str | None) -> Reels:
    """릴스 영상·썸네일 URL 교체 (렌더 완료·편집본 갱신, flush만 — commit은 서비스가).

    thumbnail_url 은 항상 그대로 덮어쓴다. 추출 실패(None)일 때 옛 값을 남기면
    영상은 편집본인데 썸네일만 편집 전 장면을 가리키게 되므로(앞부분을 잘라낸
    편집이면 영상에 없는 장면이다), 그럴 바엔 비워서 앱이 폴백하게 둔다.
    """
    reels.url = url
    reels.thumbnail_url = thumbnail_url
    db.flush()
    return reels


def soft_delete(db: Session, reels: Reels) -> None:
    """릴스 소프트 삭제 (flush만 — commit은 서비스가)."""
    reels.deleted_at = func.now()
    db.flush()


def hard_delete(db: Session, reels: Reels) -> None:
    """릴스 행을 실제로 지운다 (flush만 — commit은 서비스가).

    렌더 실패로 영상이 끝내 없는 자리표 행 전용이다 — 사용자 컨텐츠가 아니라
    렌더 시작 때 PK 를 발급하려고 미리 만든 행이라 흔적을 남길 이유가 없다
    (소프트 삭제 불변식의 의도적 예외, station·train_stop 과 같은 성격).
    comment/like 가 그 릴스를 참조하고 있으면 FK 위반으로 실패하므로 호출부에서
    소프트 삭제로 물러설 것.
    """
    db.delete(reels)
    db.flush()


def get_random_reels(
    db: Session,
    count: int,
    exclude_idxs: list[int],
    exclude_user_idxs: list[int] | None = None,
) -> list[tuple[Reels, str | None, str | None]]:
    """무작위 count개를 (릴스, 작성자 닉네임, 프로필 사진)으로 조회 (soft-delete·exclude_idxs 제외).

    작성자 없는(사진만 렌더 시절)·탈퇴한 작성자의 릴스도 나오도록 User 는 outer join —
    그런 릴스는 닉네임·프로필이 None (탈퇴 조건은 ON 절에 둬야 릴스가 통째로 빠지지 않는다).
    url 이 빈 문자열인 행은 렌더가 아직 안 끝난 자리표라 피드에서 제외한다.
    exclude_user_idxs(차단한 사용자)의 릴스는 쿼리에서 제외한다.
    """
    query = (
        db.query(Reels, User.nickname, User.profile_image)
        .outerjoin(
            User,
            (User.user_idx == Reels.user_idx) & User.deleted_at.is_(None),
        )
        .filter(Reels.deleted_at.is_(None), Reels.url != "")
    )
    if exclude_idxs:
        query = query.filter(Reels.reels_idx.notin_(exclude_idxs))
    if exclude_user_idxs:
        # user_idx 가 NULL 인 익명 릴스는 NOT IN 이 NULL 이라 통째로 빠진다 — is_(None) 로 살린다.
        query = query.filter(
            Reels.user_idx.is_(None) | Reels.user_idx.notin_(exclude_user_idxs)
        )
    return query.order_by(func.random()).limit(count).all()


def list_by_user(
    db: Session, user_idx: int, limit: int, cursor_idx: int | None = None
) -> list[Reels]:
    """내가 올린 릴스를 최신순으로 조회 (soft-delete 제외).

    작성자가 호출자 한 명뿐이라 User 조인 없이 릴스만 돌려준다 — 닉네임·프로필은
    서비스가 current_user 에서 채운다.
    cursor_idx 가 있으면 그보다 작은 PK 만 — notification_log 와 같은 커서 페이징이다.
    url 이 빈 문자열인 행은 렌더가 아직 안 끝난 자리표라 피드와 똑같이 제외한다
    (재생도 썸네일도 안 되는 카드가 마이페이지 그리드에 뚫린 칸으로 남는다).
    """
    query = db.query(Reels).filter(
        Reels.user_idx == user_idx,
        Reels.deleted_at.is_(None),
        Reels.url != "",
    )
    if cursor_idx is not None:
        query = query.filter(Reels.reels_idx < cursor_idx)
    return query.order_by(Reels.reels_idx.desc()).limit(limit).all()


def list_liked_by_user(
    db: Session,
    user_idx: int,
    limit: int,
    cursor_idx: int | None = None,
    exclude_user_idxs: list[int] | None = None,
) -> list[tuple[int, Reels, str | None, str | None]]:
    """내가 좋아요한 릴스를 **좋아요를 누른 순**으로 (좋아요 PK, 릴스, 닉네임, 프로필) 조회.

    **커서가 reels_idx 가 아니라 likes_idx 인 이유**: 정렬 기준이 릴스가 만들어진 순서가
    아니라 내가 저장한 순서라, 커서도 같은 축이어야 페이지 경계가 어긋나지 않는다.
    좋아요 취소는 행 삭제(소프트 삭제 아님)라 커서가 가리키던 행이 사라져도 부등호
    비교라 다음 페이지는 정상이다.

    Like.deleted_at 은 **걸러내지 않는다** — 좋아요 취소가 행 삭제여서 소프트 삭제된
    좋아요란 게 없고, like_dao 의 다른 읽기(counts_by_reels·liked_reels_idxs)도 같은
    이유로 안 건다. 여기서만 거르면 목록엔 없는데 like_count 에는 잡히는 릴스가 생긴다.

    댓글 좋아요(reels_idx 가 NULL)는 Reels 조인에서 자연히 빠진다. 작성자가 없는(옛
    익명 릴스)·탈퇴한 작성자의 릴스도 남도록 User 는 outer join 이다.
    """
    query = (
        db.query(Like.likes_idx, Reels, User.nickname, User.profile_image)
        .join(Reels, Reels.reels_idx == Like.reels_idx)
        .outerjoin(
            User,
            (User.user_idx == Reels.user_idx) & User.deleted_at.is_(None),
        )
        .filter(
            Like.user_idx == user_idx,
            Reels.deleted_at.is_(None),
            Reels.url != "",
        )
    )
    if cursor_idx is not None:
        query = query.filter(Like.likes_idx < cursor_idx)
    if exclude_user_idxs:
        # 익명 릴스(user_idx NULL)가 NOT IN 의 NULL 로 통째로 빠지지 않게 is_(None) 로 살린다.
        query = query.filter(
            Reels.user_idx.is_(None) | Reels.user_idx.notin_(exclude_user_idxs)
        )
    return query.order_by(Like.likes_idx.desc()).limit(limit).all()
