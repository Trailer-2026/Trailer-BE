from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.response import CommonResponse
from core.security import get_current_user
from databases.database import get_db
from databases.models.user import User
from schemas.scenic_spot_schema import (
    ScenicPlanCalibrateRequest,
    ScenicPlanResponse,
    ScenicSpotNearbyResponse,
)
from services import scenic_plan_service, scenic_spot_service

router = APIRouter(prefix="/api/scenic-spots", tags=["관광지 수집"])


@router.get(
    "/nearby",
    summary="구간별 창밖 관광지 조회",
    description=(
        "출발역→도착역 구간에서 현재 위치 기준 1500m 이내 + 진행 방향(도착역 방위) ±100°(이미 지나간 뒤편 제외) "
        "관광지를 거리순 top3로 반환합니다. 창밖 좌/우(side: left|right)는 진행 방향 기준이며, "
        "to_station은 다음 정차역 권장. 보이는 관광지가 없으면 items는 빈 배열입니다. "
        "based_at은 서버가 조회한 시각(KST)이며, '오전 9:00 기준' 같은 표시 문구는 프론트가 이 값으로 포맷팅합니다.\n\n"
        "**조회 전용입니다 — 이 API는 푸시를 보내지 않습니다.** 풍경 알림은 서버가 열차 시각표로 "
        "직접 계산해 발송하므로(`GET /api/scenic-spots/plan` 참고) 앱이 폴링할 필요가 없습니다. "
        "(access token 인증 필요)\n\n"
        "- 401: 인증 필요"
    ),
    response_model=CommonResponse[ScenicSpotNearbyResponse]
)
def get_nearby(
    lat: float = Query(..., description="현재 위도(거리 계산 기준)", example=36.59683),
    lng: float = Query(..., description="현재 경도(거리 계산 기준)", example=127.33874),
    from_station: str = Query(..., description="출발역", example="오송역"),
    to_station: str = Query(..., description="도착역", example="대전역"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = scenic_spot_service.find_nearby(db, lat, lng, from_station, to_station)
    return CommonResponse.success_response("구간별 창밖 관광지 조회 성공", data=result)


@router.get(
    "/plan",
    summary="풍경 알림 시각표 조회",
    description=(
        "지금 타고 있는(또는 3시간 안에 출발할) 열차가 **가는 길에 지날 풍경 구간 전체**를 "
        "통과 예정 시각과 함께 반환합니다. 서버가 이 시각표대로 푸시를 보내므로 앱은 아무것도 "
        "하지 않아도 알림을 받고, 이 응답은 알림 화면 상단 카드를 그리거나 로컬 알림을 미리 "
        "예약해 더 촘촘히 안내하고 싶을 때 씁니다.\n\n"
        "`eta`는 역 간 직선거리에 비례해 소요 시간을 나눈 **추정값**이라 몇 분 오차가 있습니다 "
        "(중간역 통과 시각을 주는 데이터가 없습니다). 정확도를 올리려면 "
        "`POST /api/scenic-spots/plan/calibrate`로 현재 좌표를 보내세요.\n\n"
        "`is_sent`가 true인 구간은 서버가 이미 푸시를 보냈다는 뜻이라 앱이 다시 안내할 필요가 없습니다. "
        "해당 열차가 없으면 ride는 null, items는 빈 배열입니다. (access token 인증 필요)\n\n"
        "- 401: 인증 필요"
    ),
    response_model=CommonResponse[ScenicPlanResponse],
)
def get_plan(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = scenic_plan_service.get_plan(db, current_user)
    return CommonResponse.success_response("풍경 알림 시각표 조회 성공", data=result)


@router.post(
    "/plan/calibrate",
    summary="풍경 알림 시각표 GPS 보정",
    description=(
        "현재 좌표로 열차 지연을 계산해 **남은 풍경 알림 시각을 통째로 밉니다.** 앱이 "
        "포그라운드로 올라올 때 한 번씩 보내면 됩니다.\n\n"
        "서버는 보낸 좌표를 열차 경로에 투영해 '예정대로라면 지금은 몇 시'를 구하고, 실제 "
        "시각과의 차이를 그 탑승의 지연으로 기억합니다. 응답의 `delay_minutes`가 그 값이며 "
        "(양수 = 늦음), items의 eta는 이미 보정이 반영된 시각입니다.\n\n"
        "좌표가 경로에서 20km 넘게 벗어났거나 차이가 90분을 넘으면 잘못된 매칭으로 보고 "
        "보정하지 않습니다. 그 경우에도 에러가 아니라 보정 없는 시각표를 그대로 돌려줍니다 — "
        "응답 형식은 `GET /api/scenic-spots/plan`과 완전히 같습니다.\n\n"
        "지연 보정값은 서버 메모리에만 있어 서버가 재시작하면 원래 예정 시각으로 돌아갑니다. "
        "(access token 인증 필요)\n\n"
        "- 401: 인증 필요"
    ),
    response_model=CommonResponse[ScenicPlanResponse],
)
def calibrate_plan(
    req: ScenicPlanCalibrateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = scenic_plan_service.calibrate(db, current_user, req.lat, req.lng)
    return CommonResponse.success_response("풍경 알림 시각표 보정 성공", data=result)
