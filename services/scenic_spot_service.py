from sqlalchemy.orm import Session

from databases.daos import scenic_spot_dao
from services import push_service
from utils.scenic import scenery_image_url
from utils.timezone import now_kst


def find_nearby(
    db: Session, user, lat: float, lng: float, from_station: str, to_station: str,
):
    """출발역→도착역 구간에서 보이는 관광지를 거리순 top3로 반환하고 풍경 알림을 보낸다.

    진행 방향에 맞춰 창밖 좌/우(side)를 하나로 확정해 매핑한다.
    알림은 부가 기능이라(push_service가 예외를 삼킨다) 발송에 실패해도 조회 결과는 그대로 나간다.

    일러스트는 DB가 아니라 카테고리에서 유도하는 표시용 값이라 DAO가 아닌 여기서 채운다.
    푸시 배너도 같은 값을 쓰므로(notify_scenery가 items[0]에서 읽는다) 알림보다 먼저 채운다.
    """
    based_at = now_kst()
    items = scenic_spot_dao.search_on_segment(
        db, lat, lng, from_station, to_station, top_n=3,
    )
    for item in items:
        item["image_url"] = scenery_image_url(item["category"])
    push_service.notify_scenery(db, user, items, to_station)

    return {
        "based_at": based_at,
        "feature_count": len(items),
        "items": items,
    }
