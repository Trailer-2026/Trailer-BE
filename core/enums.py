import enum


class Theme(str, enum.Enum):
    """선호하는 여행지 타입. 추천 엔진·스키마·TourAPI 분류 매핑이 공유한다.

    value는 영문 키, 한글 라벨은 THEME_LABELS로 매핑한다. (관광 데이터는 실시간
    TourAPI 호출이라 DB ENUM 타입은 더 이상 쓰지 않는다.)
    """

    NATURE = "NATURE"          # 산/자연
    OCEAN = "OCEAN"            # 바다/해안
    HISTORY = "HISTORY"        # 역사/유적
    CITY = "CITY"              # 도시
    HEALING = "HEALING"        # 힐링/온천
    FOOD = "FOOD"              # 맛집/미식
    CULTURE = "CULTURE"        # 문화/예술
    THEME_PARK = "THEME_PARK"  # 테마파크


# 테마 한글 라벨 (응답·추천 이유 표시용)
THEME_LABELS = {
    Theme.NATURE: "자연",
    Theme.OCEAN: "바다",
    Theme.HISTORY: "역사",
    Theme.CITY: "도시",
    Theme.HEALING: "힐링",
    Theme.FOOD: "미식",
    Theme.CULTURE: "문화예술",
    Theme.THEME_PARK: "테마파크",
}
