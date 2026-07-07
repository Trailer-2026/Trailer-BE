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
                "도착지별 **통합 여정(itineraries)** 을 반환합니다. 각 여정은 가는 기차 → (경유역 관광) → "
                "목적지 'N박N일' 관광·숙소 → 오는 기차를 **하나의 시간순 세그먼트(segments)** 로 엮은 것입니다. "
                "세그먼트 종류(kind): train(탑승 열차) / visit(방문 관광지, 경유역·목적지 공통) / lodging(숙소).\n\n"
                "- **도착역 지정 시**: 그 도착지로 가는 경로별 여정(직통/경유…) (destinations 길이 1).\n"
                "- **도착역 미지정 시 `dest_station_idx`: null**: theme+party(인원 구성) 기준으로 서로 다른 권역의 "
                "도착지 후보 최대 3곳을 자동 선정하고, 후보마다 경로별 여정을 반환합니다.\n\n"
                "테마(themes)는 다음 중 선택합니다: "
                "NATURE(자연), OCEAN(바다), HISTORY(역사), CITY(도시), "
                "HEALING(힐링), FOOD(미식), CULTURE(문화예술), THEME_PARK(테마파크).\n\n"
                "- 404: 출발역 없음\n"
                "- 400: 날짜 형식 오류, 오는날<가는날, 지정한 도착역 좌표 없음\n"
                "- 도착역 미지정 시 운행역을 못 찾으면 인근 대도시(KTX)로 폴백합니다.\n"
                "- 기차 경로 조회 실패(키 미설정 등) 시에도 코스는 제공되며 note로 표기됩니다.",
    response_model=CommonResponse[RecommendResponse],
)
async def recommend_courses(
    criteria: SearchCriteria,
    db: Session = Depends(get_db),
):
    result = recommend_service.recommend_courses(db, criteria)
    return CommonResponse.success_response("추천 코스 생성 성공", data=result)
