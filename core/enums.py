import enum

from sqlalchemy.dialects.postgresql import ENUM


class Theme(str, enum.Enum):
    """선호하는 여행지 타입 (PostgreSQL ENUM `theme`).

    User.theme(선호 타입)·Place.themes(추천지 태그)·추천 엔진이 공유한다.
    정의 순서는 SQL ENUM 순서와 일치해야 한다. value는 한글이 아닌 영문 키로
    저장하며, 한글 라벨은 프론트/응답에서 매핑한다(주석 참고).
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


# PostgreSQL 네이티브 ENUM 타입. station.py의 train_grade/rail_region과 동일하게
# 실제 타입(theme)은 외부 DDL에서 생성하므로 create_type=False (중복 생성 방지).
# 이 프로젝트는 레포에 .sql/ create_all이 없고 DDL을 DB에 직접 적용한다.
# 신규 `theme` 타입 생성 DDL 예시:
#   CREATE TYPE theme AS ENUM
#     ('NATURE','OCEAN','HISTORY','CITY','HEALING','FOOD','CULTURE','THEME_PARK');
_theme_enum = ENUM(
    Theme,
    name="theme",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)
