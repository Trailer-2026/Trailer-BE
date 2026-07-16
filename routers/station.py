from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.response import CommonResponse
from databases.database import get_db
from schemas.station_schema import StationResponse
from services import station_service

router = APIRouter(prefix="/api/stations", tags=["Station"])


@router.get(
    "",
    summary="역 조회/검색",
    description="기차역 목록을 역명 가나다순으로 반환합니다. query가 없으면 전체 역을, "
                "있으면 역명 부분일치로 검색합니다('부산' → '부산역'). initial을 주면 "
                "초성(ㄱㄴㄷ)으로 필터합니다(쌍자음은 대표 자음으로 묶임). 둘 다 주면 AND. "
                "initial이 자음 한 글자가 아니면 400을 반환합니다.",
    response_model=CommonResponse[list[StationResponse]],
)
def get_stations(
    query: str | None = Query(None, description="역명 검색어(부분일치). 없으면 전체 목록"),
    initial: str | None = Query(None, description="초성 필터(예: ㄱ, ㄴ, ㄷ …)"),
    db: Session = Depends(get_db),
):
    stations = station_service.search_stations(db, query, initial)
    return CommonResponse.success_response("역 목록 조회 성공", data=stations)
