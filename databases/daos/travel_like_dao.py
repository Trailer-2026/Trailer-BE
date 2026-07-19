from sqlalchemy.orm import Session

from databases.models.travel_like import TravelLike


def get(db: Session, user_idx: int, travel_idx: int) -> TravelLike | None:
    """사용자가 그 여행에 누른 좋아요 1건. 없으면 None."""
    return (
        db.query(TravelLike)
        .filter(
            TravelLike.user_idx == user_idx,
            TravelLike.travel_idx == travel_idx,
        )
        .first()
    )


def create(db: Session, user_idx: int, travel_idx: int) -> TravelLike:
    """여행 좋아요 1건 생성. flush만 하고 commit은 서비스가 한다."""
    like = TravelLike(user_idx=user_idx, travel_idx=travel_idx)
    db.add(like)
    db.flush()
    return like


def delete(db: Session, like: TravelLike) -> None:
    """좋아요 취소 = 행 삭제(소프트 삭제 아님 — 유니크 제약과 충돌하고 재좋아요가 흔하다)."""
    db.delete(like)
    db.flush()


def liked_travel_idxs(db: Session, user_idx: int, travel_idxs: list[int]) -> set[int]:
    """그 사용자가 좋아요한 여행 PK 집합 (목록 조회에서 '내가 누른 좋아요' 표시용)."""
    if not travel_idxs:
        return set()
    rows = (
        db.query(TravelLike.travel_idx)
        .filter(TravelLike.user_idx == user_idx, TravelLike.travel_idx.in_(travel_idxs))
        .all()
    )
    return {travel_idx for (travel_idx,) in rows}
