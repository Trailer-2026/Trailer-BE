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


class NotificationType(str, enum.Enum):
    """푸시 알림 종류. notification_log.type에 value가 그대로 저장된다.

    수신 설정(notification 테이블)과의 매핑은 services/push_service._ALARM_FIELD 참조.
    """

    TRAVEL_SAVED = "TRAVEL_SAVED"        # 일정에 추가되었습니다
    TRAVEL_D1 = "TRAVEL_D1"              # 일정이 하루 남았습니다
    TRAVEL_DELETED = "TRAVEL_DELETED"    # 일정에서 삭제되었습니다
    TRAIN_D10M = "TRAIN_D10M"            # 열차 출발 10분 전 (추천 코스·직접 입력 승차권 공통)
    SCENERY = "SCENERY"                  # 창밖 풍경 (이력은 중복 판정용, 알림 화면 목록엔 안 뜬다)
    STAMP_EARNED = "STAMP_EARNED"        # 마이페이지 스탬프 획득


class TravelSource(str, enum.Enum):
    """여행이 저장된 경로. travel.source에 value가 그대로 들어간다.

    'AI 추천 코스로 여행 완료' 스탬프가 이걸로 판정한다. 컬럼이 생기기 전에 저장된
    여행은 null이라 어느 쪽도 아니다(스탬프 판정에서 빠진다).
    """

    RECOMMEND = "RECOMMEND"  # 추천 플랜을 골라 저장 (POST /api/travels)
    MANUAL = "MANUAL"        # 직접 일정 만들기


class StampType(str, enum.Enum):
    """마이페이지 스탬프 종류. 조건 판정은 services/stamp_service가 한다.

    DB에 적립하지 않고 조회할 때마다 계산하므로 여기 value는 앱과의 계약(아이콘 매칭용
    키)일 뿐이다. 순서가 곧 화면에 찍히는 순서다.
    """

    FIRST_TRAIN_TRIP = "FIRST_TRAIN_TRIP"    # 첫 기차여행
    AI_COURSE_DONE = "AI_COURSE_DONE"        # AI 추천 코스로 여행 완료
    FIVE_CITIES = "FIVE_CITIES"              # 서로 다른 도시 5곳 여행
    TEN_ATTRACTIONS = "TEN_ATTRACTIONS"      # 지역 명소 10곳 방문
    TWENTY_STATIONS = "TWENTY_STATIONS"      # 기차역 20곳 방문
    MUGUNGHWA = "MUGUNGHWA"                  # 무궁화호 1회 이용
    SCENERY_PHOTOS = "SCENERY_PHOTOS"        # 풍경 사진 10장 촬영
    FIVE_REELS = "FIVE_REELS"                # 여행 영상 5개 제작
    FOUR_SEASONS = "FOUR_SEASONS"            # 봄·여름·가을·겨울 여행 완료


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
