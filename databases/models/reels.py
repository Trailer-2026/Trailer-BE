from sqlalchemy import Column, Integer, String, ForeignKey

from databases.models.base import BaseModel


class Reels(BaseModel):
    """여행 릴스 — 짧은 영상 1개.

    photos-only 렌더로 자동 생성되며, 여행(travel)과 직접 연결하지 않고
    작성자(user_idx)로만 매핑한다. user_idx 는 렌더 요청자의 JWT 에서 뽑는다.
    옛 익명 릴스가 남아있을 수 있어 NULL 허용이다.
    """

    __tablename__ = "reels"
    __table_args__ = ({"comment": "릴스"},)

    reels_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    user_idx = Column(
        Integer, ForeignKey("user.user_idx"), nullable=True, index=True,
        comment="FK 작성자 (렌더 요청자의 JWT user_idx, 옛 익명 릴스는 NULL)",
    )
    url = Column(String(100), nullable=False, comment="영상 URL")
    title = Column(String(100), nullable=True, comment="제목")
    # 홈 화면 카드용 — 카드는 영상을 안 틀고 썸네일 한 장 + 지역 태그로 그린다.
    # 둘 다 렌더 시점에만 채울 수 있어(좌표·여행 정보가 그 때만 있다) 옛 릴스는 NULL 이다.
    region = Column(String(50), nullable=True, comment="지역 태그 (예: 강원, 부산)")
    thumbnail_url = Column(String(200), nullable=True, comment="대표 프레임 이미지 URL")
    # ponytail: 좋아요 수는 reels_like COUNT(*)로 계산. 피드가 느려지면 like_count 캐시 컬럼 추가.
