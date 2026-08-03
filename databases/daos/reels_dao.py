from sqlalchemy import func
from sqlalchemy.orm import Session

from databases.models.reels import Reels
from databases.models.user import User


def get_by_idx(db: Session, reels_idx: int) -> Reels | None:
    """reels_idx로 단건 조회 (soft-delete 제외)."""
    return db.query(Reels).filter(
        Reels.reels_idx == reels_idx,
        Reels.deleted_at.is_(None),
    ).first()


def create(db: Session, *, user_idx: int | None, url: str, title: str | None) -> Reels:
    """릴스 행 생성 (flush만 — commit은 서비스가)."""
    reels = Reels(user_idx=user_idx, url=url, title=title)
    db.add(reels)
    db.flush()
    return reels


def update_url(db: Session, reels: Reels, url: str) -> Reels:
    """릴스 영상 URL 교체 (렌더 완료·편집본 갱신, flush만 — commit은 서비스가)."""
    reels.url = url
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
