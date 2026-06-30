from sqlalchemy import BigInteger, Column, Float, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import ARRAY

from core.enums import _theme_enum
from databases.models.base import BaseModel


class Place(BaseModel):
    """추천지 마스터 (AI 여행 코스 추천의 후보 풀).

    추천 엔진(recommend/)이 읽는 입력 소스이며, 사용자가 코스를 '일정표에 추가'하면
    선택된 방문지가 `schedule`(스케줄) 행으로 스냅샷 저장된다(후속 단계).
    창밖 경관 전용인 `scenic_spot`과 성격이 다르므로 별도 테이블로 둔다.

    데이터 출처는 한국관광공사 국문 관광정보 서비스(TourAPI, data.go.kr 15101578).
    content_id(관광공사 contentid)로 idempotent 업서트한다(scenic_spot.osm_uid와 동일 역할).

    DDL은 station과 동일하게 외부에서 적용한다(레포에 .sql/create_all 없음). 예시:
      CREATE TABLE place (
        place_idx    BIGSERIAL PRIMARY KEY,
        content_id   VARCHAR(20) UNIQUE,
        name         VARCHAR(255) NOT NULL,
        region       VARCHAR(100),
        lat          DOUBLE PRECISION NOT NULL,
        lng          DOUBLE PRECISION NOT NULL,
        themes       theme[] NOT NULL DEFAULT '{}',
        avg_stay_min INTEGER NOT NULL,
        image_url    VARCHAR(500),
        description  VARCHAR(255),
        created_at   TIMESTAMPTZ DEFAULT now(),
        updated_at   TIMESTAMPTZ,
        deleted_at   TIMESTAMPTZ
      );
      CREATE INDEX idx_place_themes ON place USING gin (themes);
    """

    __tablename__ = "place"
    __table_args__ = (
        Index("idx_place_themes", "themes", postgresql_using="gin"),
        {"comment": "추천지 마스터(추천 코스 후보 풀)"},
    )

    place_idx = Column(BigInteger, primary_key=True, autoincrement=True, comment="PK")
    content_id = Column(
        String(20),
        unique=True,
        comment="TourAPI(한국관광공사) contentid. idempotent 업서트 키",
    )
    name = Column(String(255), nullable=False, comment="이름")
    region = Column(String(100), comment="지역 표시용 (예: 강원특별자치도 강릉시)")
    lat = Column(Float, nullable=False, comment="위도 (k-means·거리 계산용)")
    lng = Column(Float, nullable=False, comment="경도")
    themes = Column(
        ARRAY(_theme_enum),
        nullable=False,
        server_default=text("'{}'"),
        comment="테마 태그 목록",
    )
    avg_stay_min = Column(Integer, nullable=False, comment="평균 체류시간(분)")
    image_url = Column(String(500), comment="대표 이미지 URL(TourAPI firstimage)")
    description = Column(String(255), comment="한 줄 소개(정적)")
