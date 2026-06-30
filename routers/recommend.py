from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.response import CommonResponse
from databases.database import get_db
from schemas.recommend_schema import RecommendResponse, SearchCriteria
from services import recommend_service

router = APIRouter(prefix="/api/recommend", tags=["Recommend"])


@router.post(
    "/courses",
    summary="AI 여행 코스 추천",
    description="출발역·날짜·인원·테마·추가조건(최대 이동시간/경유지/내일로)을 받아 "
                "도착지(지정 또는 AI 자동 선택) 기준 'N박N일' 순환 코스 후보(A/B/C)와 "
                "출발↔도착 왕복 기차 경로를 함께 반환합니다.\n\n"
                "테마(themes)는 다음 중 선택합니다: "
                "NATURE(자연), OCEAN(바다), HISTORY(역사), CITY(도시), "
                "HEALING(힐링), FOOD(미식), CULTURE(문화예술), THEME_PARK(테마파크).\n\n"
                "- 404: 출발/도착역 없음, 도착역 결정 불가(운행역 정보 없음)\n"
                "- 400: 날짜 형식 오류, 오는날<가는날, 도착역 좌표 없음\n"
                "- 기차 경로 조회 실패(키 미설정 등) 시에도 코스는 제공되며 note로 표기됩니다.",
    response_model=CommonResponse[RecommendResponse],
)
async def recommend_courses(
    criteria: SearchCriteria,
    db: Session = Depends(get_db),
):
    result = recommend_service.recommend_courses(db, criteria)
    return CommonResponse.success_response("추천 코스 생성 성공", data=result)
