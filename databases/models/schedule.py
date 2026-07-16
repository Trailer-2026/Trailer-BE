from sqlalchemy import Column, Integer, SmallInteger, String, Time, Float, ForeignKey

from databases.models.base import BaseModel


class Schedule(BaseModel):
    """여행 하루 안의 일정 항목 1개 (기차/방문지/숙소 공통) — 시간순 타임라인의 한 행.

    추천 여정(Itinerary)의 세그먼트 1개 = schedule 1행으로 저장된다. 종류는 kind
    (train|visit|lodging)로 구분하고, 기차는 dep_station/arr_station/train_no/train_grade에
    구조화해 담는다(title은 'KTX 101 서울→부산' 형태 표시용 폴백). 좌표는 방문지·숙소는 자기
    좌표, 기차는 출발역 좌표(station 테이블)로 채운다. day_no+sequence로 그날 순서를 표현.
    """

    __tablename__ = "schedule"
    __table_args__ = ({"comment": "스케줄"},)

    schedule_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    travel_idx = Column(
        Integer, ForeignKey("travel.travel_idx"), nullable=False, index=True, comment="FK 여행"
    )
    user_idx = Column(
        Integer, ForeignKey("user.user_idx"), nullable=False, index=True, comment="FK 사용자"
    )
    day_no = Column(SmallInteger, nullable=False, comment="여행 일자 (day1=1)")
    sequence = Column(SmallInteger, nullable=False, comment="그날의 n번째 일정")
    kind = Column(
        String(10), nullable=False, server_default="visit",
        comment="항목 종류 (train | visit | lodging)",
    )
    title = Column(String(100), nullable=False, comment="장소명/일정명")
    train_no = Column(String(10), nullable=True, comment="열차번호 (kind=train만)")
    train_grade = Column(String(20), nullable=True, comment="열차 등급 (kind=train만, 예: KTX)")
    dep_station = Column(String(50), nullable=True, comment="출발역명 (kind=train만, 접미사 '역' 없음)")
    arr_station = Column(String(50), nullable=True, comment="도착역명 (kind=train만, 접미사 '역' 없음)")
    start_time = Column(Time, nullable=False, comment="시작 시간")
    end_time = Column(Time, nullable=False, comment="종료 시간")
    latitude = Column(Float, nullable=False, comment="위도")
    longitude = Column(Float, nullable=False, comment="경도")
    image_url = Column(String(255), nullable=True, comment="추천 관광지/숙소 대표 이미지 URL")
    memo = Column(String, nullable=True, comment="메모")
