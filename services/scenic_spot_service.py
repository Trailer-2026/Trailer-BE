from sqlalchemy.orm import Session

from databases.daos import scenic_spot_dao
from utils.scenic import scenery_image_url
from utils.timezone import now_kst


def find_nearby(
    db: Session, lat: float, lng: float, from_station: str, to_station: str,
):
    """출발역→도착역 구간에서 보이는 관광지를 거리순 top3로 반환한다.

    진행 방향에 맞춰 창밖 좌/우(side)를 하나로 확정해 매핑한다.

    **알림은 여기서 보내지 않는다.** 전에는 이 조회가 발송 지점이었는데, 앱이 물어봐야만
    나가는 구조라 폴링이 걸린 화면을 열지 않은 사용자에게는 한 건도 가지 않았다. 지금은
    서버가 시각표를 계산해 스스로 보낸다(services/scenic_plan_service). 이 엔드포인트는
    화면에 지금 보이는 것을 그리는 조회 전용이다.

    일러스트는 DB가 아니라 카테고리에서 유도하는 표시용 값이라 DAO가 아닌 여기서 채운다.
    푸시 배너도 같은 함수로 고르므로 조회 카드와 알림 그림이 항상 같다.
    """
    based_at = now_kst()
    items = scenic_spot_dao.search_on_segment(
        db, lat, lng, from_station, to_station, top_n=3,
    )
    for item in items:
        item["image_url"] = scenery_image_url(item["category"])

    return {
        "based_at": based_at,
        "feature_count": len(items),
        "items": items,
    }
