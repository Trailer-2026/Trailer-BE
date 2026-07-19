from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.response import CommonResponse
from core.security import get_current_user
from databases.database import get_db
from databases.models.user import User
from schemas.travel_schema import (
    HomeTravelCard,
    PastTravelListResponse,
    TravelCreateRequest,
    TravelDetailResponse,
    TravelLikeResponse,
    TravelResponse,
    TravelTicketsResponse,
)
from services import travel_like_service, travel_service

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


# 아래 "/{travel_idx}"보다 먼저 선언해야 한다 — 순서가 바뀌면 "past"가 int 경로에 걸려 422.
@router.get(
    "/past",
    summary="지난 여행 목록 조회",
    description="로그인한 사용자의 '지난 여행'(이미 종료된 여행)을 종료일 내림차순으로 반환합니다"
                "(여행기록 화면 지난 여행 섹션용). "
                "종료 여부는 여행 종료일과 오늘(KST)로 계산하며, 종료일이 오늘 이전인 여행만 담습니다 — "
                "진행 중·예정 여행은 포함되지 않아 status는 항상 COMPLETED입니다. "
                "카드 썸네일은 여행의 첫 일정 대표 이미지이고, liked는 내가 하트를 누른 여행인지 여부입니다. "
                "지난 여행이 없으면 빈 배열을 반환합니다.\n\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[PastTravelListResponse],
)
def get_past_travels(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.past_travels(db, current_user)
    return CommonResponse.success_response("지난 여행 조회 성공", data=result)


@router.get(
    "/{travel_idx}",
    summary="여행 일정표 상세 조회",
    description="여행 1건의 일정표를 일자별 타임라인으로 반환합니다(내 일정 > 일정표 탭). "
                "추천 코스 저장의 응답으로 받은 `travel_idx`를 파라미터로 이용합니다."
                "일정 항목을 day_no(DAY)로 묶고 각 일자의 항목은 sequence 오름차순으로 정렬합니다. "
                "각 일자의 날짜는 여행 시작일 + (day_no-1)로 계산하며, 기차 항목의 title은 "
                "'KTX 101 서울→부산' 형태입니다. status는 여행 기간과 오늘(KST)로 계산됩니다.\n\n"
                "- 404: 존재하지 않거나 본인 여행이 아님\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TravelDetailResponse],
)
def get_travel_detail(
    travel_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.travel_detail(db, current_user, travel_idx)
    return CommonResponse.success_response("여행 일정표 조회 성공", data=result)


@router.get(
    "/{travel_idx}/tickets",
    summary="승차권 조회",
    description="여행의 승차권 목록을 반환합니다(승차권 화면용). "
                "AI 추천 일정을 승인(추천 코스 저장)한 여행에서만 조회할 수 있습니다 — "
                "승인 시 발급받은 `travel_idx`를 파라미터로 이용합니다. "
                "승차권 1매 = 기차 일정 1건이며, 승차 일자·출발/도착역·출발/도착 시각·열차 등급/번호를 담습니다. "
                "좌석·호차·타는곳 번호 등 예매 정보는 제공하지 않습니다. 기차 일정이 없으면 빈 배열입니다.\n\n"
                "- 404: 승인(저장)된 일정이 아니거나 본인 여행이 아님\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TravelTicketsResponse],
)
def get_travel_tickets(
    travel_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.travel_tickets(db, current_user, travel_idx)
    return CommonResponse.success_response("승차권 조회 성공", data=result)


@router.post(
    "/{travel_idx}/likes",
    summary="여행 좋아요",
    description="여행 1건에 좋아요(하트)를 누릅니다(여행기록 > 지난 여행 카드용). "
                "토글이 아니라서 이미 좋아요한 여행에 다시 요청해도 에러 없이 liked=true를 반환합니다(멱등). "
                "본인 여행에만 누를 수 있습니다.\n\n"
                "- 404: 존재하지 않거나 본인 여행이 아님\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TravelLikeResponse],
)
def like_travel(
    travel_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_like_service.like_travel(db, current_user, travel_idx)
    return CommonResponse.success_response("여행 좋아요 성공", data=result)


@router.delete(
    "/{travel_idx}/likes",
    summary="여행 좋아요 취소",
    description="여행 1건의 좋아요(하트)를 취소합니다. "
                "좋아요하지 않은 여행에 요청해도 에러 없이 liked=false를 반환합니다(멱등).\n\n"
                "- 404: 존재하지 않거나 본인 여행이 아님\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TravelLikeResponse],
)
def unlike_travel(
    travel_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_like_service.unlike_travel(db, current_user, travel_idx)
    return CommonResponse.success_response("여행 좋아요 취소 성공", data=result)
