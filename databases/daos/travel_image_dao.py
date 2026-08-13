from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from databases.models.travel_image import TravelImage


def by_travel(db: Session, travel_idx: int) -> list[TravelImage]:
    """여행에 붙은 사진 전부를 올린 순(image_idx)으로. 일정에 안 붙은 사진(schedule_idx=NULL)도 포함.

    여행 상세·영상 렌더 둘 다 여행 1건의 사진을 통째로 쓰므로 조회는 이 하나뿐이고,
    일정별로 나누는 건 호출부가 schedule_idx로 묶는다(쿼리 1회, N+1 없음).
    """
    return (
        db.query(TravelImage)
        .filter(
            TravelImage.travel_idx == travel_idx,
            TravelImage.deleted_at.is_(None),
        )
        .order_by(TravelImage.image_idx)
        .all()
    )


def create(db: Session, travel_idx: int, schedule_idx: int | None, url: str) -> TravelImage:
    """여행에 사진 1행 첨부 (schedule_idx는 선택). flush만 하고 commit은 서비스가 한다."""
    image = TravelImage(travel_idx=travel_idx, schedule_idx=schedule_idx, url=url)
    db.add(image)
    db.flush()
    return image


def detach_schedule(db: Session, schedule_idx: int) -> None:
    """일정이 삭제될 때 거기 붙은 사진을 여행 소속으로 떼어 놓는다(schedule_idx=NULL). flush만.

    안 떼면 사진이 삭제된 일정을 계속 가리켜 여행 상세·영상 어디에도 안 나오는데 행과
    저장소 객체는 남는다(사용자가 image_idx를 볼 수 없어 지울 수도 없다). 사진의 소유는
    일정이 아니라 여행이므로 일정만 사라지고 사진은 남는 게 맞다.
    """
    (
        db.query(TravelImage)
        .filter(
            TravelImage.schedule_idx == schedule_idx,
            TravelImage.deleted_at.is_(None),
        )
        .update({TravelImage.schedule_idx: None}, synchronize_session=False)
    )
    db.flush()


def get_by_idx(db: Session, image_idx: int) -> TravelImage | None:
    """image_idx로 단건 조회. 삭제용 — 이미 지운 사진은 안 잡힌다(두 번째 삭제는 404)."""
    return (
        db.query(TravelImage)
        .filter(
            TravelImage.image_idx == image_idx,
            TravelImage.deleted_at.is_(None),
        )
        .first()
    )


def delete(db: Session, image: TravelImage) -> None:
    """사진 1행 소프트 삭제 — deleted_at을 찍는다. flush만 하고 commit은 서비스가 한다."""
    image.deleted_at = func.now()
    db.flush()
