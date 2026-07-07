from fastapi import APIRouter, Query

from core.enums import Theme
from core.response import CommonResponse
from schemas.place_schema import ThemedPlacesResponse
from services import place_service

router = APIRouter(prefix="/api/places", tags=["Place"])


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
