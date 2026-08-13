from sqlalchemy import Column, Integer, String, ForeignKey

from databases.models.base import BaseModel


class TravelImage(BaseModel):
    """사용자가 여행 중 찍어 올린 사진 1장.

    소유는 여행(travel_idx, 필수)이고, 일정(schedule_idx)은 **선택**이다 — 사용자가 일정을
    골라 올리면 그 일정에, 그냥 여행에 올리면 NULL로 둔다. 영상 렌더는 일정에 붙은 사진을
    그 일정 좌표에서 보여주고, 일정 없는 사진은 보여줄 좌표가 없어 마지막 지점에 몰아 붙인다.

    schedule.image_url(관광지 대표 이미지)·travel.cover_image_url(카드 썸네일)과는 다른
    것이다 — 영상에 들어가는 건 이 테이블뿐이다.

    사용자가 만든 데이터라 다른 테이블과 같이 BaseModel을 상속해 소프트 삭제한다
    (삭제 = deleted_at 기록, 읽기는 deleted_at IS NULL 필터). station·train_stop 같은
    참조 데이터 스냅샷만이 하드 삭제의 예외이고 이 테이블은 거기 해당하지 않는다.
    """

    __tablename__ = "travel_image"
    __table_args__ = ({"comment": "여행 이미지"},)

    image_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    travel_idx = Column(
        Integer,
        ForeignKey("travel.travel_idx"),
        nullable=False,
        index=True,
        comment="FK 여행",
    )
    schedule_idx = Column(
        Integer,
        ForeignKey("schedule.schedule_idx"),
        nullable=True,
        index=True,
        comment="FK 스케줄 (일정을 고르지 않고 올린 사진은 NULL)",
    )
    url = Column(String(255), nullable=False, comment="이미지 경로")
