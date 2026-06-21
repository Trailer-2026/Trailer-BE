from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.response import CommonResponse
from databases.database import get_db
from schemas.scenic_spot_schema import ScenicSpotNearbyResponse
from services import scenic_spot_service

router = APIRouter(prefix="/api/scenic-spots", tags=["관광지 수집"])


@router.get(
    "/nearby",
    summary="구간별 창밖 관광지 조회",
    description=(
        "출발역→도착역 구간에서 보이는 관광지를 진행 방향 기준 창밖 좌/우(side: left|right)로 "
        "확정해 거리순 top3로 반환합니다. from_station/to_station은 출발역→도착역 순서로 넘기며, "
        "방향을 뒤집으면 좌/우도 반대로 매핑됩니다. 좌/우는 노선과 무관한 기하 속성이라 노선은 받지 않으며, "
        "역쌍이 여러 노선에 걸리면 현재 위치에서 가장 가까운 트랙 기준으로 해소합니다. "
        "매칭되는 구간이 없으면 items는 빈 배열입니다."
    ),
    response_model=CommonResponse[ScenicSpotNearbyResponse]
)
async def get_nearby(
    lat: float = Query(..., description="현재 위도(거리 계산 기준)", example=36.331894),
    lng: float = Query(..., description="현재 경도(거리 계산 기준)", example=127.434522),
    from_station: str = Query(..., description="출발역", example="오송"),
    to_station: str = Query(..., description="도착역", example="대전"),
    db: Session = Depends(get_db),
):
    result = scenic_spot_service.find_nearby(db, lat, lng, from_station, to_station)
    return CommonResponse.success_response("구간별 창밖 관광지 조회 성공", data=result)
