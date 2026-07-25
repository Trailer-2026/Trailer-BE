from sqlalchemy.orm import Session

from databases.models.travel_image import TravelImage


def list_by_schedule_idxs(db: Session, schedule_idxs: list[int]) -> list[TravelImage]:
    """여러 스케줄의 첨부 이미지를 IN 일괄 조회 (N+1 회피).

    travel_image 는 감사 컬럼 없는 최소 구조(Base 직접 상속)라 soft-delete 필터가 없다.
    """
    if not schedule_idxs:
        return []
    return (
        db.query(TravelImage)
        .filter(TravelImage.schedule_idx.in_(schedule_idxs))
        .order_by(TravelImage.schedule_idx, TravelImage.image_idx)
        .all()
    )
