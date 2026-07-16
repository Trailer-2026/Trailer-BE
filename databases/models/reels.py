from sqlalchemy import Column, Integer, String, ForeignKey

from databases.models.base import BaseModel


class Reels(BaseModel):
    """여행 릴스 — 확정된 여행(travel) 1건에 사용자가 올린 짧은 영상 1개."""

    __tablename__ = "reels"
    __table_args__ = ({"comment": "릴스"},)

    reels_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    travel_idx = Column(
        Integer, ForeignKey("travel.travel_idx"), nullable=False, index=True, comment="FK 여행"
    )
    user_idx = Column(
        Integer, ForeignKey("user.user_idx"), nullable=False, index=True, comment="FK 작성자"
    )
    url = Column(String(100), nullable=False, comment="영상 URL")
    title = Column(String(100), nullable=True, comment="제목")
    # ponytail: 좋아요 수는 reels_like COUNT(*)로 계산. 피드가 느려지면 like_count 캐시 컬럼 추가.
