from sqlalchemy import Column, Integer, String, ForeignKey

from databases.models.base import BaseModel


class Reels(BaseModel):
    """여행 릴스 — 짧은 영상 1개.

    photos-only 렌더로 자동 생성되며 로그인한 작성자에 연결된다. 옛 익명 릴스가
    남아있을 수 있어 user_idx 는 NULL 허용이다.
    """

    __tablename__ = "reels"
    __table_args__ = ({"comment": "릴스"},)

    reels_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    user_idx = Column(
        Integer, ForeignKey("user.user_idx"), nullable=True, index=True,
        comment="FK 작성자 (옛 익명 릴스는 NULL)",
    )
    url = Column(String(100), nullable=False, comment="영상 URL")
    title = Column(String(100), nullable=True, comment="제목")
    # ponytail: 좋아요 수는 reels_like COUNT(*)로 계산. 피드가 느려지면 like_count 캐시 컬럼 추가.
