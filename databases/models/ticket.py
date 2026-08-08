from sqlalchemy import BigInteger, Column, Date, ForeignKey, Index, Integer, String, Time

from databases.models.base import BaseModel


class Ticket(BaseModel):
    """사용자가 직접 입력해 저장한 승차권 1매 ('티켓 정보 추가하기' 화면).

    AI 추천 코스를 저장할 때 생기는 승차권(schedule kind=train)과는 별개다 — 추천 코스대로
    움직이는 사용자는 추천이 준 열차 정보를 그대로 쓰고, 이 테이블은 추천 없이 승차권만
    저장하거나 추천과 별개로 예매 정보를 적어 두는 경우를 담는다. 둘을 합쳐 보여주지 않는다.

    여행(travel)에 묶지 않는다. 여행을 하나도 만들지 않은 사용자도 승차권만 저장할 수 있어야
    해서 소유자는 user_idx 하나다. 같은 이유로 여행 기간 검증도 하지 않는다.

    출발·도착은 날짜와 시각을 나눠 담는다 — 화면 입력 단위가 그렇고, travel.start_date·
    schedule.start_time과 같은 naive KST wall-clock 표현을 유지한다. 열차번호·등급은 화면에
    입력칸이 없어 저장하지 않는다.
    """

    __tablename__ = "ticket"
    __table_args__ = (
        # 목록 조회 질의(user_idx로 좁히고 출발 일시 오름차순) 그대로를 태운다.
        Index("ix_ticket_user_dep", "user_idx", "dep_date", "dep_time"),
        # 탑승 알림 배치(ticket_dao.list_departing_on)는 사용자를 가리지 않고 날짜로만
        # 좁힌다 — 위 인덱스는 선두 컬럼이 user_idx라 그 조회엔 못 쓴다. 1분마다 도는
        # 조회라 승차권이 쌓이면 seq scan 비용이 그대로 누적되므로 날짜 단독으로 하나 더 둔다.
        Index("ix_ticket_dep_date", "dep_date"),
        {"comment": "직접 입력 승차권"},
    )

    ticket_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    user_idx = Column(
        Integer, ForeignKey("user.user_idx"), nullable=False, index=True, comment="FK 사용자"
    )
    # station.station_idx가 BigInteger라 FK 컬럼도 같은 타입이어야 한다.
    dep_station_idx = Column(
        BigInteger, ForeignKey("station.station_idx"), nullable=False, comment="FK 출발역"
    )
    arr_station_idx = Column(
        BigInteger, ForeignKey("station.station_idx"), nullable=False, comment="FK 도착역"
    )
    dep_date = Column(Date, nullable=False, comment="출발일")
    dep_time = Column(Time, nullable=False, comment="출발 시각")
    arr_date = Column(Date, nullable=False, comment="도착일 (출발일과 다를 수 있다 — 자정 넘김)")
    arr_time = Column(Time, nullable=False, comment="도착 시각")
    car_no = Column(String(10), nullable=True, comment="호차 번호 (선택)")
    seat_no = Column(String(10), nullable=True, comment="좌석 번호 (선택)")
