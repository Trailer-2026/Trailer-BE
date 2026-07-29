from fastapi import APIRouter, Query

from core.enums import Theme
from core.response import CommonResponse
from schemas.place_schema import PlaceSearchResponse, ThemedPlacesResponse
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
