from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.response import CommonResponse
from databases.database import get_db
from schemas.train_schema import TrainSearchResponse
from services import train_service

router = APIRouter(prefix="/api/trains", tags=["Train"])


@router.get(
    "/search",
    summary="열차 시간표 검색",
    description="출발역(dep)→도착역(arr) 구간의 date(YYYYMMDD) 운행 열차를 출발시각순으로 "
                "반환합니다. 등급·시각·소요시간·운임 포함. time(HH:MM)을 주면 그 시각 이후 "
                "출발 열차만 반환합니다. nail_pass=true면 내일로패스 적용 열차"
                "(무궁화·누리로·ITX 계열만, KTX 계열·SRT 제외)만 반환합니다. "
                "왕복은 가는편/오는편을 각각 호출하세요. 결과 없음은 빈 배열입니다.\n\n"
                "- 400: date/time 형식 오류 또는 시간표 미지원 역(nat_code 없음)\n"
                "- 404: 존재하지 않는 역\n"
                "- 502: 외부 열차정보 API 호출 실패",
    response_model=CommonResponse[list[TrainSearchResponse]],
)
def search_trains(
    dep: int = Query(..., description="출발역 station_idx"),
    arr: int = Query(..., description="도착역 station_idx"),
    date: str = Query(..., description="운행일자 (YYYYMMDD)"),
    time: str | None = Query(None, description="이 시각(HH:MM) 이후 출발 열차만. 없으면 하루 전체"),
    nail_pass: bool = Query(False, description="내일로패스 적용 열차만(KTX 계열·SRT 제외)"),
    db: Session = Depends(get_db),
):
    trains = train_service.search_trains(db, dep, arr, date, time, nail_pass)
    return CommonResponse.success_response("열차 조회 성공", data=trains)
