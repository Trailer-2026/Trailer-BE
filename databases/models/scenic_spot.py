from sqlalchemy import Column, Integer, String, Float
from sqlalchemy.orm import relationship
from databases.models.base import BaseModel


class ScenicSpot(BaseModel):
    __tablename__ = 'scenic_spot'
    __table_args__ = {'comment': 'Overpass로 수집한 관광지'}

    scenic_spot_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    osm_uid = Column(String(50), nullable=False, unique=True, index=True, comment="OSM 식별자(type/id)")
    category = Column(String(50), nullable=False, comment="분류")
    name = Column(String(255), nullable=True, comment="이름")
    lat = Column(Float, nullable=False, comment="위도")
    lng = Column(Float, nullable=False, comment="경도")

    # 이 관광지가 보이는 기차 노선 구간/방향 (1:N). 기본 lazy=select → top-N 조회 시에만 로드
    segments = relationship("ScenicSpotSegment", cascade="all, delete-orphan")
