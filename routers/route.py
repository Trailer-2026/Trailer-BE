from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.response import CommonResponse
from databases.database import get_db
from schemas.route_schema import RouteCandidate
from services import route_service

router = APIRouter(prefix="/api/routes", tags=["Route"])


@router.get(
    "/recommend",
    summary="기차 경로 추천 (직통/경유/왕복)",
    description="출발역(dep)→도착역(arr)을 가는날(go_date)·오는날(back_date) 기준으로 "
                "직통/경유 왕복 경로 후보를 반환합니다. 경유는 경로상 주요역에 2시간 이상 "
                "체류하는 안을 총 이동시간순으로 제시합니다. 첫 번째 항목은 항상 직통 왕복이며 "
                "(무조건 리턴 보장), 직통 열차가 없으면 note로 표기됩니다. 관광지·예산은 미포함.\n\n"
                "- 400: 날짜/시간 형식 오류, 오는날<가는날, 경로탐색 미지원 역(nat_code 없음)\n"
                "- 404: 존재하지 않는 역\n"
                "- 502: 외부 열차정보 API 호출 실패",
    response_model=CommonResponse[list[RouteCandidate]],
)
def recommend_routes(
    dep: int = Query(..., description="출발역 station_idx"),
    arr: int = Query(..., description="도착역 station_idx"),
    go_date: str = Query(..., description="가는날 (YYYYMMDD)"),
    back_date: str = Query(..., description="오는날 (YYYYMMDD)"),
    go_time: str | None = Query(None, description="가는 편 최소 출발시각(HH:MM). 기본 09:00"),
    back_time: str | None = Query(None, description="오는 편 최소 출발시각(HH:MM). 기본 09:00"),
    db: Session = Depends(get_db),
):
    routes = route_service.recommend(db, dep, arr, go_date, back_date, go_time, back_time)
    return CommonResponse.success_response("경로 추천 성공", data=routes)
