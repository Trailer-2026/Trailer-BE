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
        "출발역→도착역 구간에서 현재 위치 기준 1500m 이내 + 진행 방향(도착역 방위) ±100°(이미 지나간 뒤편 제외) "
        "관광지를 거리순 top3로 반환합니다. 창밖 좌/우(side: left|right)는 진행 방향 기준이며, "
        "to_station은 다음 정차역 권장. 보이는 관광지가 없으면 items는 빈 배열입니다. "
        "based_at은 서버가 조회한 시각(KST)이며, '오전 9:00 기준' 같은 표시 문구는 프론트가 이 값으로 포맷팅합니다."
    ),
    response_model=CommonResponse[ScenicSpotNearbyResponse]
)
def get_nearby(
    lat: float = Query(..., description="현재 위도(거리 계산 기준)", example=36.59683),
    lng: float = Query(..., description="현재 경도(거리 계산 기준)", example=127.33874),
    from_station: str = Query(..., description="출발역", example="오송역"),
    to_station: str = Query(..., description="도착역", example="대전역"),
    db: Session = Depends(get_db),
):
    result = scenic_spot_service.find_nearby(db, lat, lng, from_station, to_station)
    return CommonResponse.success_response("구간별 창밖 관광지 조회 성공", data=result)
