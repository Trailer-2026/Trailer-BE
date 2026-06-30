from sqlalchemy import BigInteger, Column, Float, String

from databases.models.base import BaseModel


class Lodging(BaseModel):
    """숙소 마스터 (TourAPI 숙박, contentTypeId=32).

    추천지(place)와 달리 테마가 없다. 코스의 각 거점(이동한 지역)마다 가장 가까운
    숙소 1곳을 붙이는 용도. content_id(관광공사 contentid)로 idempotent 업서트.
    """

    __tablename__ = "lodging"
    __table_args__ = ({"comment": "숙소(TourAPI 숙박 32)"},)

    lodging_idx = Column(BigInteger, primary_key=True, autoincrement=True, comment="PK")
    content_id = Column(String(20), unique=True, comment="TourAPI contentid")
    name = Column(String(255), nullable=False, comment="이름")
    lodging_type = Column(String(40), comment="유형(관광호텔/펜션/게스트하우스 등)")
    region = Column(String(100), comment="주소")
    lat = Column(Float, nullable=False, comment="위도")
    lng = Column(Float, nullable=False, comment="경도")
    tel = Column(String(60), comment="전화번호")
    image_url = Column(String(500), comment="대표 이미지 URL")
