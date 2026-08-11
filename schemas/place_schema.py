from pydantic import BaseModel, Field

from core.enums import Theme


class PlaceBase(BaseModel):
    """추천 장소 표시 공통 필드 — RecommendedPlace(코스 방문지)·StopoverPlace(경유역 관광지) 공유.

    두 타입은 preference_score/reason/visit_time만 맥락별로 다르게 재정의하고 나머지는 이 베이스를 상속한다.
    (recommend_schema→route_schema 단방향 의존이라, 순환을 피하려 공통 베이스를 leaf 모듈인 여기 둔다.)
    _stopover_to_place가 둘을 변환하므로 표시 필드는 항상 호환돼야 한다 — 그 계약을 여기서 강제한다.
    """

    place_idx: int = Field(..., description="추천지 PK")
    name: str = Field(..., description="이름")
    region: str | None = Field(None, description="지역")
    lat: float = Field(..., description="위도")
    lng: float = Field(..., description="경도")
    themes: list[Theme] = Field(..., description="테마 태그")
    image_url: str | None = Field(None, description="대표 이미지 URL")
    open_time: str | None = Field(None, description="운영 시작 시각 (HH:MM). 미상이면 null")
    close_time: str | None = Field(None, description="운영 종료 시각 (HH:MM). 미상이면 null")


class PlaceSearchResult(BaseModel):
    """장소 키워드 검색 결과 1건 — 일정 항목(visit) 추가 시 그대로 채워 보낸다."""

    name: str = Field(..., description="장소명", examples=["감천문화마을"])
    address: str | None = Field(None, description="도로명(없으면 지번) 주소", examples=["부산 사하구 감내2로 203"])
    category: str | None = Field(None, description="카테고리(예: 관광명소, 음식점). 없으면 null", examples=["관광명소"])
    latitude: float = Field(..., description="위도", examples=[35.0975])
    longitude: float = Field(..., description="경도", examples=[129.0107])


class PlaceSearchResponse(BaseModel):
    """장소 키워드 검색 응답 — 검색어에 대한 후보 목록."""

    places: list[PlaceSearchResult] = Field(..., description="검색 결과(정확도순). 없으면 빈 배열")


class ThemePlaceCard(BaseModel):
    """홈 '테마별 여행지' 리스트의 관광지 1곳 — 카드에 보이는 필드 + 상세 진입용 ID."""

    content_id: str = Field(
        ...,
        description="TourAPI 콘텐츠 ID. 카드를 누르면 `GET /api/places/{content_id}`로 상세를 조회한다",
        examples=["1623750"],
    )
    name: str = Field(..., description="관광지명")
    region: str | None = Field(None, description="지역 시도·시군구. 없으면 null")
    image_url: str | None = Field(None, description="대표 이미지 URL")


class ThemedPlacesResponse(BaseModel):
    """홈 '테마별 여행지' 섹션 — 한 테마의 배너 문구 + 전국 관광지 목록."""

    theme: Theme = Field(..., description="이번에 표시된 테마 키 (서버가 랜덤 선택했을 수 있음)")
    title: str = Field(..., description="배너 문구 (예: '아름다운 숲과 자연의 도시')")
    banner_image_url: str | None = Field(None, description="배너 이미지 (대표 관광지 이미지에서 파생). 없으면 null")
    places: list[ThemePlaceCard] = Field(..., description="이 테마의 전국 관광지 목록")


class NearestStationInfo(BaseModel):
    """관광지에서 가장 가까운 기차역 — 상세 화면의 '대전역에서 도보 7~8분' 한 줄."""

    station_idx: int = Field(..., description="역 PK", examples=[12])
    station_name: str = Field(..., description="역명('역' 접미사 포함)", examples=["대전역"])
    distance_m: int = Field(..., description="관광지↔역 직선거리(m)", examples=[520])
    walk_minutes: int | None = Field(
        None,
        description="도보 예상 시간(분). 걸어갈 만한 거리(1.5km 이내)일 때만 채워지고 그 밖이면 null. "
                    "직선거리에 우회 보정(1.2배)을 곱해 4.8km/h로 환산한 근사값",
        examples=[8],
    )
    text: str = Field(
        ...,
        description="그대로 노출하는 완성 문구. 도보권이면 '{역명}에서 도보 N~N+1분', "
                    "아니면 '{역명}에서 N.Nkm'",
        examples=["대전역에서 도보 8~9분"],
    )


class NearbyRestaurant(BaseModel):
    """상세 화면 '가까운 맛집' 카드 1장."""

    content_id: str = Field(..., description="TourAPI 콘텐츠 ID", examples=["3063000"])
    name: str = Field(..., description="음식점명", examples=["삼부자 부대찌개"])
    category: str | None = Field(
        None, description="음식 종류(한식·일식·카페/전통찻집 등). 미분류면 null", examples=["한식"],
    )
    address: str | None = Field(
        None, description="주소", examples=["대전광역시 유성구 어은로52번길 5 (어은동)"],
    )
    image_url: str | None = Field(None, description="대표 사진. 없으면 null")
    distance_m: int = Field(..., description="관광지로부터 직선거리(m)", examples=[455])
    latitude: float = Field(..., description="위도", examples=[36.3628])
    longitude: float = Field(..., description="경도", examples=[127.3626])


class PlaceDetailResponse(BaseModel):
    """'테마별 여행지' 카드를 눌렀을 때의 상세 화면 — 지역 소개 + 가까운 맛집."""

    content_id: str = Field(..., description="TourAPI 콘텐츠 ID", examples=["1623750"])
    name: str = Field(..., description="관광지명", examples=["갑천"])
    headline: str = Field(
        ...,
        description="상단 큰 제목. 테마 카피와 관광지명을 엮은 서버 소유 문구",
        examples=["아름다운 숲과 자연의 도시, 갑천"],
    )
    themes: list[Theme] = Field(..., description="테마 태그. 어느 테마에도 안 걸리면 빈 배열")
    address: str | None = Field(
        None, description="주소", examples=["대전광역시 유성구 구성동"],
    )
    latitude: float = Field(..., description="위도", examples=[36.3628])
    longitude: float = Field(..., description="경도", examples=[127.3626])
    images: list[str] = Field(
        ..., description="사진 URL 목록(대표 사진이 항상 첫 장). 없으면 빈 배열",
    )
    overview: str | None = Field(
        None, description="소개글(HTML 태그 제거된 평문, 문단 줄바꿈은 보존). 없으면 null",
    )
    tel: str | None = Field(None, description="전화번호. 없으면 null")
    homepage: str | None = Field(None, description="홈페이지 URL. 없으면 null")
    nearest_station: NearestStationInfo | None = Field(
        None, description="가장 가까운 기차역. 역 데이터가 없으면 null",
    )
    restaurants: list[NearbyRestaurant] = Field(
        ...,
        description="가까운 맛집(사진 있는 곳 우선·거리순). 반경 2km→5km→10km 순으로 넓히며 "
                    "처음 결과가 나온 반경만 쓰므로, 도심은 대부분 2km 안이고 주변에 등록 "
                    "음식점이 드문 곳만 멀어진다(distance_m로 확인). 없으면 빈 배열",
    )
