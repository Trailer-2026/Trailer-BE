"""여행 좋아요 서비스 — 지난 여행 카드의 하트. 트랜잭션(commit)은 이 레이어가 소유한다.

토글이 아니라 POST(좋아요)/DELETE(취소)로 나눈다 — 재시도해도 상태가 뒤집히지 않는다.
이미 좋아요한 여행에 다시 POST하거나, 안 누른 여행에 DELETE해도 에러 없이 현재 상태를 돌려준다(멱등).
좋아요는 본인 여행에만 누를 수 있고, 타인·미존재 여행은 404다(403 아님 — 존재 여부를 흘리지 않는다).
"""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.exceptions.custom import NotFoundException
from databases.daos import travel_dao, travel_like_dao
from schemas.travel_schema import TravelLikeResponse


def like_travel(db: Session, user, travel_idx: int) -> TravelLikeResponse:
    """여행 좋아요. 이미 눌렀으면 그대로 둔다."""
    _travel_or_404(db, user, travel_idx)
    _insert_like(db, user.user_idx, travel_idx)
    return TravelLikeResponse(travel_idx=travel_idx, liked=True)


def unlike_travel(db: Session, user, travel_idx: int) -> TravelLikeResponse:
    """여행 좋아요 취소. 안 눌렀으면 아무것도 안 한다."""
    _travel_or_404(db, user, travel_idx)
    like = travel_like_dao.get(db, user.user_idx, travel_idx)
    if like is not None:
        travel_like_dao.delete(db, like)
        db.commit()
    return TravelLikeResponse(travel_idx=travel_idx, liked=False)


def _insert_like(db: Session, user_idx: int, travel_idx: int) -> None:
    """좋아요 행 삽입. 이미 있으면(선조회로 걸리든, 동시 요청과 경합하든) 아무 일도 없다.

    하트 더블탭처럼 두 요청이 동시에 오면 둘 다 '아직 없음'을 보고 INSERT해 유니크 제약에
    걸린다(UniqueViolation → 500). 그건 에러가 아니라 '이미 좋아요됨'이므로 삼키고 성공 처리.
    """
    if travel_like_dao.get(db, user_idx, travel_idx) is not None:
        return
    try:
        travel_like_dao.create(db, user_idx, travel_idx)
        db.commit()
    except IntegrityError:
        db.rollback()  # 경합에서 진 쪽 — 상대가 이미 넣었다


def _travel_or_404(db: Session, user, travel_idx: int) -> None:
    """본인 여행인지까지 확인. 타인 여행도 404로 막는다(travel_detail과 같은 관례)."""
    travel = travel_dao.get_by_idx(db, travel_idx)
    if travel is None or travel.user_idx != user.user_idx:
        raise NotFoundException("여행을 찾을 수 없습니다.")
