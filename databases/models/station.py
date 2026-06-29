import enum

from sqlalchemy import BigInteger, Boolean, Column, Computed, Float, Index, String, text
from sqlalchemy.dialects.postgresql import ARRAY, ENUM

from databases.models.base import BaseModel


class TrainGrade(str, enum.Enum):
    """열차 등급 (PostgreSQL ENUM `train_grade`). 정의 순서는 SQL ENUM 순서와 일치해야 함."""

    SRT = "SRT"
    KTX = "KTX"
    KTX_SANCHEON = "KTX-산천"
    KTX_CHEONGRYONG = "KTX-청룡"
    KTX_EUM = "KTX-이음"
    ITX_SAEMAEUL = "ITX-새마을"
    SAEMAEUL = "새마을호"
    ITX_MAEUM = "ITX-마음"
    ITX_CHEONGCHUN = "ITX-청춘"
    MUGUNGHWA = "무궁화호"
    NURIRO = "누리로"


class RailRegion(str, enum.Enum):
    """관리 본부 (PostgreSQL ENUM `rail_region`). 8개 본부."""

    SEOUL = "서울본부"
    METRO = "수도권광역본부"
    GANGWON = "강원본부"
    DAEJEON_CHUNGCHEONG = "대전충청본부"
    JEONBUK = "전북본부"
    GWANGJU_JEONNAM = "광주전남본부"
    DAEGU_GYEONGBUK = "대구경북본부"
    BUSAN_GYEONGNAM = "부산경남본부"


# PostgreSQL 네이티브 ENUM 타입. 실제 타입(train_grade / rail_region)은
# train_station_postgres.sql 에서 생성하므로 create_type=False (중복 생성 방지).
# values_callable: Python 멤버명이 아닌 한글 value("KTX-산천" 등)를 그대로 저장.
_train_grade_enum = ENUM(
    TrainGrade,
    name="train_grade",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)
_rail_region_enum = ENUM(
    RailRegion,
    name="rail_region",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)


class Station(BaseModel):
    __tablename__ = "station"
    __table_args__ = (
        Index("idx_station_grades", "grades", postgresql_using="gin"),
        Index("idx_station_is_ktx", "is_ktx", postgresql_where=text("is_ktx")),
        {"comment": "기차역"},
    )

    station_idx = Column(BigInteger, primary_key=True, autoincrement=True, comment="PK")
    station_name = Column(String(100), nullable=False, unique=True, comment="역명")
    nat_code = Column(
        String(12),
        unique=True,
        comment="TAGO 열차정보 API(GetStrtpntAlocFndTrainInfo) 역코드(NAT). 일부 역(예: 평택지제) 미존재→NULL",
    )
    latitude = Column(Float, comment="위도")
    longitude = Column(Float, comment="경도")
    region = Column(_rail_region_enum, comment="관리 본부")
    grades = Column(
        ARRAY(_train_grade_enum),
        nullable=False,
        server_default=text("'{}'"),
        comment="정차 열차 등급 목록",
    )
    is_ktx = Column(
        Boolean,
        Computed(
            "grades && ARRAY['KTX','KTX-산천','KTX-청룡','KTX-이음']::train_grade[]",
            persisted=True,
        ),
        comment="KTX 계열(KTX·산천·청룡·이음) 정차 여부 (생성 컬럼)",
    )