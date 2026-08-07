from datetime import date, time

from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql import func

from databases.models.station import Station
from databases.models.ticket import Ticket


def create(
    db: Session, user_idx: int, dep_station_idx: int, arr_station_idx: int,
    dep_date: date, dep_time: time, arr_date: date, arr_time: time,
    car_no: str | None = None, seat_no: str | None = None,
) -> Ticket:
    """직접 입력 승차권 1행 생성. flush만 하고 commit은 서비스가 한다."""
    ticket = Ticket(
        user_idx=user_idx,
        dep_station_idx=dep_station_idx, arr_station_idx=arr_station_idx,
        dep_date=dep_date, dep_time=dep_time, arr_date=arr_date, arr_time=arr_time,
        car_no=car_no, seat_no=seat_no,
    )
    db.add(ticket)
    db.flush()
    return ticket


def list_by_user(db: Session, user_idx: int) -> list[tuple[Ticket, str, str]]:
    """사용자의 승차권을 출발 일시 오름차순으로 조회 (soft-delete 제외).

    역명은 station을 두 번(출발·도착) 조인해 한 번에 가져온다 — 승차권마다 역을 다시
    조회하면 N+1이 된다. 반환은 (승차권, 출발역명, 도착역명) 튜플.
    """
    dep = aliased(Station)
    arr = aliased(Station)
    return (
        db.query(Ticket, dep.station_name, arr.station_name)
        .join(dep, Ticket.dep_station_idx == dep.station_idx)
        .join(arr, Ticket.arr_station_idx == arr.station_idx)
        .filter(
            Ticket.user_idx == user_idx,
            Ticket.deleted_at.is_(None),
        )
        .order_by(Ticket.dep_date, Ticket.dep_time, Ticket.ticket_idx)
        .all()
    )


def get_owned(db: Session, user_idx: int, ticket_idx: int) -> Ticket | None:
    """본인 승차권 단건 조회. 없거나 남의 것이면 None (soft-delete 제외)."""
    return (
        db.query(Ticket)
        .filter(
            Ticket.ticket_idx == ticket_idx,
            Ticket.user_idx == user_idx,
            Ticket.deleted_at.is_(None),
        )
        .first()
    )


def soft_delete(db: Session, ticket: Ticket) -> None:
    """승차권 소프트 삭제 (deleted_at 세팅). flush만, commit은 서비스."""
    ticket.deleted_at = func.now()
    db.flush()
