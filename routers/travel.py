from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.response import CommonResponse
from core.security import get_current_user
from databases.database import get_db
from databases.models.user import User
from schemas.travel_schema import HomeTravelCard, TravelCreateRequest, TravelResponse
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
def create_travel(
    req: TravelCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.save_selected_plan(db, current_user, req.plan_id)
    return CommonResponse.success_response("여행 저장 성공", data=result)


@router.get(
    "/current",
    summary="홈 화면 여행 카드 조회",
    description="로그인한 사용자의 '지금/곧 떠나는 여행' 1건을 반환합니다(홈 화면 여행 카드용). "
                "진행 중(ONGOING) 여행을 우선하고, 없으면 가장 가까운 예정(PLANNED) 여행을, 둘 다 없으면 null을 반환합니다. "
                "status는 여행 기간과 오늘(KST)로 계산됩니다 — 시작 전 PLANNED, 기간 내 ONGOING, 종료 후 COMPLETED.\n\n"
                "- data=null: 진행 중·예정 여행이 없음(홈 기본 화면 표시)\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[HomeTravelCard | None],
)
def get_current_travel(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.current_travel(db, current_user)
    return CommonResponse.success_response("홈 여행 카드 조회 성공", data=result)
