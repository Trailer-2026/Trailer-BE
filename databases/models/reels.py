from sqlalchemy import Column, Integer, String, ForeignKey

from databases.models.base import BaseModel


class Reels(BaseModel):
    """여행 릴스 — 짧은 영상 1개.

    여행(travel)과 직접 연결하지 않고 작성자(user_idx)로만 매핑한다.
    user_idx 는 렌더 요청자의 JWT 에서 뽑아 매핑한다 (과거 데이터는 NULL 가능).
    """

    __tablename__ = "reels"
    __table_args__ = ({"comment": "릴스"},)

    reels_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    user_idx = Column(
        Integer, ForeignKey("user.user_idx"), nullable=True, index=True,
        comment="FK 작성자 (렌더 요청자의 JWT user_idx, 과거 데이터는 NULL)",
    )
    url = Column(String(100), nullable=False, comment="영상 URL")
    title = Column(String(100), nullable=True, comment="제목")
    # ponytail: 좋아요 수는 reels_like COUNT(*)로 계산. 피드가 느려지면 like_count 캐시 컬럼 추가.
