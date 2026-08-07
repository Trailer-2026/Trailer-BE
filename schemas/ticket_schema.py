from datetime import date, time

from pydantic import BaseModel, Field


class TicketCreateRequest(BaseModel):
    """승차권 직접 입력 저장 요청 ('티켓 정보 추가하기' 화면).

    출발지·도착지·출발일시·도착일시가 필수, 호차·좌석은 선택이다. 경유지는 받지 않는다.
    열차번호·등급은 화면에 입력칸이 없어 저장하지 않는다.

    역은 `GET /api/stations`에서 고른 `station_idx`로 보낸다 — 역명 문자열이면 표기가
    제각각이 될 수 있어 식별자로 받는다.
    """

    dep_station_idx: int = Field(..., description="출발역 PK (GET /api/stations의 station_idx)", examples=[12])
    arr_station_idx: int = Field(..., description="도착역 PK (GET /api/stations의 station_idx)", examples=[87])
    dep_date: date = Field(..., description="출발일 (현재 이후)", examples=["2026-08-20"])
    dep_time: time = Field(..., description="출발 시각 HH:MM", examples=["11:13"])
    arr_date: date = Field(..., description="도착일 (출발일과 달라도 된다 — 자정 넘김 열차)", examples=["2026-08-21"])
    arr_time: time = Field(..., description="도착 시각 HH:MM", examples=["01:20"])
    car_no: str | None = Field(None, max_length=10, description="호차 번호 (선택)", examples=["9"])
    seat_no: str | None = Field(None, max_length=10, description="좌석 번호 (선택)", examples=["7A"])


class TicketResponse(BaseModel):
    """직접 입력해 저장한 승차권 1매."""

    ticket_idx: int = Field(..., description="승차권 PK", examples=[4])
    dep_station_idx: int = Field(..., description="출발역 PK", examples=[12])
    dep_station: str = Field(..., description="출발역명 (station 테이블 형식 — '역' 접미사 포함)", examples=["서울역"])
    arr_station_idx: int = Field(..., description="도착역 PK", examples=[87])
    arr_station: str = Field(..., description="도착역명 (station 테이블 형식 — '역' 접미사 포함)", examples=["부산역"])
    dep_date: date = Field(..., description="출발일", examples=["2026-08-20"])
    dep_time: time = Field(..., description="출발 시각 (HH:MM:SS)", examples=["11:13:00"])
    arr_date: date = Field(..., description="도착일", examples=["2026-08-21"])
    arr_time: time = Field(..., description="도착 시각 (HH:MM:SS)", examples=["01:20:00"])
    car_no: str | None = Field(None, description="호차 번호. 입력하지 않았으면 null", examples=["9"])
    seat_no: str | None = Field(None, description="좌석 번호. 입력하지 않았으면 null", examples=["7A"])


class TicketListResponse(BaseModel):
    """내 승차권 목록 — 출발 일시 오름차순(빠른 출발 먼저)."""

    tickets: list[TicketResponse] = Field(
        ..., description="승차권 목록 (출발 일시 오름차순). 저장한 승차권이 없으면 빈 배열",
    )
    total: int = Field(..., description="승차권 총 매수", examples=[2])
