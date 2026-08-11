"""마이페이지 스탬프 — 조건을 만족한 칸을 찍어 준다.

**적립 테이블이 없다.** 볼 때마다 사용자의 여행·일정·승차권·릴스를 세서 판정한다. 조회는
마이페이지 탭을 누를 때만 돌고 한 사용자 데이터만 훑는다.

조건 판정의 기준 시각은 KST 오늘이다. '다녀온 여행'은 종료일이 오늘보다 이전인 여행을
말한다 — travel.status 컬럼은 항상 PLANNED이라 쓰지 않는다(databases/daos/stamp_dao 참조).
"""
import logging
from datetime import date

from sqlalchemy.orm import Session

from core.enums import StampType
from databases.daos import stamp_dao
from schemas.stamp_schema import StampItem, StampListResponse
from utils.timezone import now_kst

logger = logging.getLogger(__name__)

# 아이콘은 GCS에 올려 두고 URL을 서버가 소유한다(문구와 마찬가지로 앱 배포 없이 바꾸려고).
_ICON_BASE = "https://storage.googleapis.com/trailer-bucket/stamp"

# 스탬프 정의 — (종류, 라벨, 조건 안내, 아이콘 슬러그, 목표치).
# **이 순서가 곧 화면 그리드 순서**라 앱이 정렬하지 않는다.
_STAMPS: tuple[tuple[StampType, str, str, str, int], ...] = (
    (StampType.FIRST_TRAIN_TRIP, "첫 기차여행",
     "기차가 포함된 여행을 한 번 다녀오면 찍혀요", "first_train_trip", 1),
    (StampType.AI_COURSE_DONE, "AI 추천 코스로 여행 완료",
     "추천받은 코스를 그대로 저장해 다녀오면 찍혀요", "ai_course_done", 1),
    (StampType.FIVE_CITIES, "서로 다른 도시 5곳 여행",
     "서로 다른 도시를 다섯 곳 다녀오면 찍혀요", "five_cities", 5),
    (StampType.TEN_ATTRACTIONS, "지역 명소 10곳 방문",
     "여행 일정에 담은 명소를 열 곳 다녀오면 찍혀요", "ten_attractions", 10),
    (StampType.TWENTY_STATIONS, "기차역 20곳 방문",
     "서로 다른 기차역 스무 곳을 거쳐 가면 찍혀요", "twenty_stations", 20),
    (StampType.MUGUNGHWA, "무궁화호 1회 이용",
     "무궁화호를 한 번 타고 다녀오면 찍혀요", "mugunghwa", 1),
    (StampType.SCENERY_PHOTOS, "풍경 사진 10장 촬영",
     "창밖 풍경 사진을 열 장 찍으면 찍혀요", "scenery_photos", 10),
    (StampType.FIVE_REELS, "여행 영상 5개 제작",
     "여행 영상을 다섯 개 만들면 찍혀요", "five_reels", 5),
    (StampType.FOUR_SEASONS, "봄·여름·가을·겨울 여행 완료",
     "네 계절에 각각 한 번씩 다녀오면 찍혀요", "four_seasons", 4),
)

# 계절 경계(월 기준). 겨울이 해를 넘어가므로 표로 둔다.
_SEASON_OF_MONTH = {
    3: "봄", 4: "봄", 5: "봄",
    6: "여름", 7: "여름", 8: "여름",
    9: "가을", 10: "가을", 11: "가을",
    12: "겨울", 1: "겨울", 2: "겨울",
}


def _city(region: str) -> str:
    """대표 지역 문자열을 도시 하나로 정규화한다.

    travel.region은 자유 문자열이라 같은 도시가 '대전'과 '대전역'으로 갈린다(추천 저장은
    지역명, 직접 만들기는 사용자 입력). 역명으로 적힌 경우가 있어 접미사 '역'을 떼고,
    '부산광역시 해운대구'처럼 긴 주소가 들어오면 첫 낱말만 남긴다.
    """
    city = (region or "").strip().split()[0] if (region or "").strip() else ""
    if len(city) > 1 and city.endswith("역"):
        city = city[:-1]
    for suffix in ("특별자치도", "특별자치시", "광역시", "특별시", "시", "도"):
        if len(city) > len(suffix) and city.endswith(suffix):
            return city[: -len(suffix)]
    return city


def _station(name: str) -> str:
    """역명을 한 가지 표기로 맞춘다.

    schedule.dep_station은 '서울', ticket을 조인한 station.station_name은 '서울역'이라
    그대로 세면 같은 역이 둘로 잡힌다. 접미사를 떼는 쪽으로 통일한다.
    """
    station = (name or "").strip()
    return station[:-1] if len(station) > 1 and station.endswith("역") else station


def _seasons(periods: list[tuple[date, date]]) -> set[str]:
    """다녀온 여행들이 걸친 계절 집합.

    시작일 한 점이 아니라 여행 기간이 걸친 달을 모두 본다 — 2월 말에 떠나 3월 초에 돌아오면
    겨울과 봄 양쪽에 다녀온 것이기 때문이다. 여행이 길어야 며칠이라 달 단위로 훑어도 싸다.
    """
    out: set[str] = set()
    for start, end in periods:
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            out.add(_SEASON_OF_MONTH[month])
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def _progress(db: Session, user_idx: int, today: date) -> dict[StampType, int]:
    """스탬프별 현재 진행 수치. 목표치로 자르는 건 호출부(list_stamps)가 한다."""
    regions = {c for c in (_city(r) for r in stamp_dao.finished_regions(db, user_idx, today)) if c}
    stations = {s for s in (_station(n) for n in stamp_dao.visited_station_names(db, user_idx, today)) if s}
    return {
        StampType.FIRST_TRAIN_TRIP: stamp_dao.count_train_rides(db, user_idx, today),
        StampType.AI_COURSE_DONE: stamp_dao.count_recommended_travels(db, user_idx, today),
        StampType.FIVE_CITIES: len(regions),
        StampType.TEN_ATTRACTIONS: stamp_dao.count_visited_attractions(db, user_idx, today),
        StampType.TWENTY_STATIONS: len(stations),
        StampType.MUGUNGHWA: stamp_dao.count_mugunghwa_rides(db, user_idx, today),
        # 촬영 기록을 남기는 곳이 아직 없다 — 사진은 앱에서 찍고 서버로는 오지 않는다.
        # 저장 경로가 생기면 여기만 실제 카운트로 바꾸면 된다(칸·아이콘은 이미 있다).
        StampType.SCENERY_PHOTOS: 0,
        StampType.FIVE_REELS: stamp_dao.count_my_reels(db, user_idx),
        StampType.FOUR_SEASONS: len(_seasons(stamp_dao.finished_travel_periods(db, user_idx, today))),
    }


def list_stamps(db: Session, user_idx: int) -> StampListResponse:
    """마이페이지 스탬프 탭 — 9칸 전부와 달성 개수를 함께 내려준다.

    미달성 칸도 빼지 않고 담는다. 화면이 잠긴 칸까지 그리드로 보여주고, 앱이 그 위에
    자물쇠 아이콘을 덮기 때문이다.

    """
    progress = _progress(db, user_idx, now_kst().date())
    items = [
        StampItem(
            type=stamp_type,
            title=title,
            description=description,
            image_url=f"{_ICON_BASE}/{slug}.png",
            achieved=progress[stamp_type] >= goal,
            progress=min(progress[stamp_type], goal),
            goal=goal,
        )
        for stamp_type, title, description, slug, goal in _STAMPS
    ]
    return StampListResponse(
        achieved_count=sum(1 for item in items if item.achieved),
        total_count=len(items),
        stamps=items,
    )
