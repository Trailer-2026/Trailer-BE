from sqlalchemy.orm import Session

from databases.models.travel_image import TravelImage


def urls_by_schedule(db: Session, schedule_idxs: list[int]) -> dict[int, list[str]]:
    """스케줄별 첨부 이미지 URL 을 IN 일괄 조회해 {schedule_idx: [url, ...]}로 반환 (N+1 회피).

    travel_image 는 감사 컬럼 없는 최소 구조(Base 직접 상속)라 soft-delete 필터가 없다.
    """
    if not schedule_idxs:
        return {}
    rows = (
        db.query(TravelImage.schedule_idx, TravelImage.url)
        .filter(TravelImage.schedule_idx.in_(schedule_idxs))
        .order_by(TravelImage.schedule_idx, TravelImage.image_idx)
        .all()
    )
    urls: dict[int, list[str]] = {}
    for schedule_idx, url in rows:
        urls.setdefault(schedule_idx, []).append(url)
    return urls
