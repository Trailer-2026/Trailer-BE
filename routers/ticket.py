from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.response import CommonResponse
from core.security import get_current_user
from databases.database import get_db
from databases.models.user import User
from schemas.ticket_schema import TicketCreateRequest, TicketListResponse, TicketResponse
from services import ticket_service

router = APIRouter(prefix="/api/tickets", tags=["Ticket"])


@router.post(
    "",
    summary="승차권 저장",
    description="승차권 1매를 직접 입력해 저장합니다(티켓 정보 추가하기 화면). "
                "출발지·도착지·출발일·출발 시간·도착일·도착 시간이 필수이고, 호차 번호·좌석 번호는 선택입니다. "
                "경유지는 받지 않습니다. 출발지·도착지는 `GET /api/stations`에서 고른 `station_idx`로 보냅니다. "
                "도착일은 출발일과 달라도 됩니다(자정을 넘기는 열차).\n\n"
                "AI 추천 코스를 저장했을 때 생기는 승차권(`GET /api/travels/{travel_idx}/tickets`)과는 "
                "**별개**입니다 — 여행에 묶이지 않고, 여행이 하나도 없어도 저장할 수 있습니다.\n\n"
                "- 400: 존재하지 않는 역 / 출발역과 도착역이 같음 / 도착 일시가 출발 일시보다 빠름 / "
                "이미 지난 출발 일시\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TicketResponse],
)
def create_ticket(
    req: TicketCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = ticket_service.create_ticket(db, current_user, req)
    return CommonResponse.success_response("승차권 저장 성공", data=result)


@router.get(
    "",
    summary="내 승차권 목록 조회",
    description="직접 입력해 저장한 승차권을 출발 일시 오름차순(빠른 출발 먼저)으로 반환합니다. "
                "역명은 저장된 역 PK로 조인해 함께 내려갑니다. 저장한 승차권이 없으면 빈 배열입니다.\n\n"
                "AI 추천 코스의 승차권은 포함되지 않습니다 — 그쪽은 "
                "`GET /api/travels/{travel_idx}/tickets`를 이용합니다.\n\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TicketListResponse],
)
def get_tickets(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = ticket_service.list_tickets(db, current_user)
    return CommonResponse.success_response("승차권 조회 성공", data=result)


@router.delete(
    "/{ticket_idx}",
    summary="승차권 삭제",
    description="직접 입력해 저장한 승차권 1매를 삭제합니다(소프트 삭제). 본인 승차권만 삭제할 수 있습니다.\n\n"
                "- 404: 존재하지 않거나 본인 승차권이 아님\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[None],
)
def delete_ticket(
    ticket_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ticket_service.delete_ticket(db, current_user, ticket_idx)
    return CommonResponse.success_response("승차권 삭제 성공")
