from sqlalchemy import Column, Integer, String, ForeignKey

from databases.models.base import Base


class TravelImage(Base):
    """스케줄에 첨부된 여행 이미지 1장.

    스케줄 1건에 여러 장을 붙일 수 있다(1:N). schedule.image_url(대표 이미지)과 별개로
    사용자가 업로드한 이미지들을 담는다. created_at 등 감사 컬럼 없이 최소 구조로 유지
    (BaseModel이 아닌 Base 직접 상속 — 소프트 삭제 불변식의 의도적 예외).
    """

    __tablename__ = "travel_image"
    __table_args__ = ({"comment": "여행 이미지"},)

    image_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    schedule_idx = Column(
        Integer,
        ForeignKey("schedule.schedule_idx"),
        nullable=False,
        index=True,
        comment="FK 스케줄",
    )
    url = Column(String(255), nullable=False, comment="이미지 경로")
