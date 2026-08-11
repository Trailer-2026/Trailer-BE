from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from core.enums import Theme
from core.response import CommonResponse
from databases.database import get_db
from schemas.place_schema import PlaceDetailResponse, PlaceSearchResponse, ThemedPlacesResponse
from services import place_service

router = APIRouter(prefix="/api/places", tags=["Place"])


@router.get(
    "/search",
    summary="장소 키워드 검색",
    description="키워드로 장소를 검색합니다(직접 일정 만들기 > 장소 추가 검색창용). "
                "카카오 로컬 검색 기반이라 관광지뿐 아니라 공항·상호 등 일반 장소도 찾습니다. "
                "결과의 name/address/latitude/longitude를 그대로 일정 항목 추가(visit)에 씁니다.\n\n"
                "- 502: 장소 검색 서비스(카카오) 호출 실패",
    response_model=CommonResponse[PlaceSearchResponse],
)
def search_places(
    query: str = Query(..., min_length=1, description="검색어(장소명/주소)"),
):
    result = place_service.search_places(query)
    return CommonResponse.success_response("장소 검색 성공", data=result)


@router.get(
    "/themed",
    summary="테마별 여행지 조회",
    description="홈 화면 '테마별 여행지' 섹션 데이터를 반환합니다. 테마의 배너 문구와 "
                "전국 관광지 목록(대표 이미지 있는 것)을 담습니다. 데이터는 실시간 TourAPI에서 옵니다.\n\n"
                "- `theme` 미지정 시 서버가 **랜덤 테마**를 고릅니다.\n"
                "- '다른 테마' 버튼은 `theme` 없이 다시 호출하면 됩니다(매번 랜덤, 간혹 같은 테마가 나올 수 있음).\n"
                "- 특정 테마를 고정해 받으려면 `theme`를 지정합니다.",
    response_model=CommonResponse[ThemedPlacesResponse],
)
def get_themed_places(
    theme: Theme | None = Query(None, description="여행지 테마. 미지정이면 서버가 랜덤 선택('다른 테마' 버튼용)"),
):
    result = place_service.themed_places(theme)
    return CommonResponse.success_response("테마별 여행지 조회 성공", data=result)


@router.get(
    "/{content_id}",
    summary="여행지 상세 조회 (지역 소개 + 가까운 맛집)",
    description="홈 '테마별 여행지' 카드를 눌렀을 때 뜨는 상세 화면 데이터입니다. "
                "`content_id`는 `GET /api/places/themed` 응답 카드의 `content_id`를 그대로 씁니다. "
                "한 화면에 다 보이는 정보라 **지역 소개와 가까운 맛집을 한 응답에** 담습니다.\n\n"
                "- **지역 소개**: `headline`(상단 큰 제목), `images`(대표 사진이 첫 장), "
                "`overview`(소개글 평문), `address`, `nearest_station`.\n"
                "- **가까운 역**: `nearest_station.text`를 그대로 노출하면 됩니다"
                "(예: '대전역에서 도보 8~9분'). 도보 시간은 직선거리 기반 근사이고, "
                "1.5km를 넘으면 `walk_minutes`가 null이 되며 문구도 km 표기로 바뀝니다.\n"
                "- **가까운 맛집**: 사진 있는 곳 우선·거리순으로 최대 `restaurant_limit`개. "
                "반경은 2km→5km→10km로 넓히다 처음 결과가 나온 곳에서 멈추므로, 시골 관광지는 "
                "수 km 떨어진 곳이 나올 수 있습니다(`distance_m` 참고).\n\n"
                "관광 정보는 실시간 TourAPI에서 오고, 역만 서버 DB에서 옵니다. "
                "맛집 조회나 역 조회가 실패해도 상세 자체는 내려갑니다(각각 빈 배열·null).\n\n"
                "- 404: 해당 콘텐츠가 TourAPI에 없거나 좌표가 없어 상세를 만들 수 없음\n"
                "- 502: 관광 정보 서비스(TourAPI) 호출 실패",
    response_model=CommonResponse[PlaceDetailResponse],
)
def get_place_detail(
    # 숫자만 허용해 위의 /search·/themed 경로와 겹치지 않게 한다(선언 순서에 기대지 않음).
    content_id: str = Path(..., pattern=r"^\d+$", description="TourAPI 콘텐츠 ID", example="1623750"),
    restaurant_limit: int = Query(6, ge=1, le=20, description="가까운 맛집 최대 개수"),
    db: Session = Depends(get_db),
):
    result = place_service.place_detail(db, content_id, restaurant_limit)
    return CommonResponse.success_response("여행지 상세 조회 성공", data=result)
