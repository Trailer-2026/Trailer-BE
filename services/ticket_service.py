"""직접 입력 승차권 서비스 — '티켓 정보 추가하기' 화면의 저장·목록·삭제.

AI 추천 코스를 저장할 때 생기는 승차권(schedule kind=train, travel_service.travel_tickets)과는
별개 도메인이다. 두 소스를 합치지 않는다 — 추천 코스대로 움직이는 사용자는 추천이 준 열차
정보를 쓰고, 여기는 추천 없이 승차권만 저장하거나 추천과 별개로 예매 정보를 적는 경우다.

여행(travel)에 묶지 않으므로 여행 기간 검증도 없다. 검증은 '출발·도착 일시가 엇갈리지 않을 것'과
'출발 일시가 아직 오지 않았을 것' 둘뿐이다.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from core.exceptions.custom import BadRequestException, NotFoundException
from databases.daos import station_dao, ticket_dao
from databases.models.ticket import Ticket
from schemas.ticket_schema import TicketCreateRequest, TicketListResponse, TicketResponse
from utils.timezone import now_kst


def create_ticket(db: Session, user, req: TicketCreateRequest) -> TicketResponse:
    """승차권 1매를 저장한다. 서비스가 트랜잭션을 소유(commit)."""
    if req.dep_station_idx == req.arr_station_idx:
        raise BadRequestException("출발역과 도착역이 같을 수 없습니다.")

    dep_station = station_dao.get_by_idx(db, req.dep_station_idx)
    arr_station = station_dao.get_by_idx(db, req.arr_station_idx)
    if dep_station is None or arr_station is None:
        raise BadRequestException("존재하지 않는 역입니다.")

    dep_at = datetime.combine(req.dep_date, req.dep_time)
    arr_at = datetime.combine(req.arr_date, req.arr_time)
    if arr_at <= dep_at:
        raise BadRequestException("도착 일시는 출발 일시보다 빠를 수 없습니다.")
    # 컬럼이 naive wall-clock(KST)이라 현재 시각도 tzinfo를 떼고 비교한다.
    if dep_at <= now_kst().replace(tzinfo=None):
        raise BadRequestException("이미 지난 승차권은 저장할 수 없습니다.")

    ticket = ticket_dao.create(
        db, user_idx=user.user_idx,
        dep_station_idx=req.dep_station_idx, arr_station_idx=req.arr_station_idx,
        dep_date=req.dep_date, dep_time=req.dep_time,
        arr_date=req.arr_date, arr_time=req.arr_time,
        car_no=req.car_no, seat_no=req.seat_no,
    )
    db.commit()
    return _to_response(ticket, dep_station.station_name, arr_station.station_name)


def list_tickets(db: Session, user) -> TicketListResponse:
    """내가 저장한 승차권 목록 — 출발 일시 오름차순. 없으면 빈 배열."""
    rows = ticket_dao.list_by_user(db, user.user_idx)
    tickets = [_to_response(t, dep_name, arr_name) for t, dep_name, arr_name in rows]
    return TicketListResponse(tickets=tickets, total=len(tickets))


def delete_ticket(db: Session, user, ticket_idx: int) -> None:
    """승차권 소프트 삭제. 없거나 남의 승차권이면 404(403 아님 — travel_service와 같은 관례)."""
    ticket = ticket_dao.get_owned(db, user.user_idx, ticket_idx)
    if ticket is None:
        raise NotFoundException("승차권을 찾을 수 없습니다.")
    ticket_dao.soft_delete(db, ticket)
    db.commit()


def _to_response(ticket: Ticket, dep_station: str, arr_station: str) -> TicketResponse:
    """Ticket 행 + 조회한 역명 → 응답 스키마. 역명은 조인/단건조회 어느 쪽에서 와도 된다."""
    return TicketResponse(
        ticket_idx=ticket.ticket_idx,
        dep_station_idx=ticket.dep_station_idx, dep_station=dep_station,
        arr_station_idx=ticket.arr_station_idx, arr_station=arr_station,
        dep_date=ticket.dep_date, dep_time=ticket.dep_time,
        arr_date=ticket.arr_date, arr_time=ticket.arr_time,
        car_no=ticket.car_no, seat_no=ticket.seat_no,
    )
