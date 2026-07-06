from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.response import CommonResponse
from core.security import get_current_user
from databases.database import get_db
from databases.models.user import User
from schemas.travel_schema import TravelCreateRequest, TravelResponse
from services import travel_service

router = APIRouter(prefix="/api/travels", tags=["Travel"])


@router.post(
    "",
    summary="추천 코스 저장",
    description="추천 응답에서 선택한 플랜의 `plan_id`를 받아 내 여행으로 저장합니다. "
                "서버가 캐시에서 그 플랜(기차·방문지·숙소)을 꺼내 Travel + 일정(schedule)으로 저장하며, "
                "제목은 플랜 제목이 기본값입니다.\n\n"
                "- 400: plan_id 캐시 만료(추천 후 시간 초과·서버 재시작) → 다시 추천받아야 합니다.\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TravelResponse],
)
async def create_travel(
    req: TravelCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.save_selected_plan(db, current_user, req.plan_id)
    return CommonResponse.success_response("여행 저장 성공", data=result)
