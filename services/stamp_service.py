"""마이페이지 스탬프 — 조건을 만족한 칸을 찍어 주고, 처음 찍힌 순간에 알림을 보낸다.

**조건은 매번 세지만, 찍혔다는 사실은 user_stamp에 남긴다.** 둘 다 필요하다 — 진행도
('10곳 중 3곳')는 원본을 세야 나오고, '획득 순간'은 기록해 두지 않으면 붙잡을 수 없다.
한 번 딴 스탬프는 조건이 나중에 어긋나도(여행을 지우는 등) 유지된다.

재판정이 도는 곳은 셋이다. 어느 쪽이 먼저 집어도 user_stamp의 유니크 제약이 '한 번만'을
지키므로 알림은 정확히 한 번 나간다.
  1) 스탬프 탭 조회(list_stamps) — 배치가 놓쳤어도 여기서 메워진다
  2) 자정 배치(award_for_finished_travels) — 앱을 안 열어도 알림이 가야 하니까
  3) 릴스 렌더 완료(video_service) — '여행 영상 5개'만은 날짜가 아니라 행동으로 켜진다

조건 판정의 기준 시각은 KST 오늘이다. '다녀온 여행'은 종료일이 오늘보다 이전인 여행을
말한다 — travel.status 컬럼은 항상 PLANNED이라 쓰지 않는다(databases/daos/stamp_dao 참조).
"""
import logging
from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.enums import NotificationType, StampType
from databases.daos import stamp_dao, user_stamp_dao
from schemas.stamp_schema import StampItem, StampListResponse
from services import push_service
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
    # 문구가 '창밖 풍경'이 아니라 '여행 사진'인 이유: 세는 대상이 travel_image(여행에 올린
    # 사진 전부)라 음식·숙소 사진도 함께 잡힌다. 창밖만 골라내려면 기차 일정에 매핑된
    # 사진만 세야 하는데, 자동 매핑(_snap_to_schedule)이 기차 일정의 좌표를 출발역으로
    # 잡는 탓에 달리는 중에 찍은 사진은 대개 근처 방문지로 스냅된다 — 조건을 제대로
    # 만족해도 안 찍히는 칸이 된다.
    (StampType.SCENERY_PHOTOS, "풍경 사진 10장 촬영",
     "여행 사진을 열 장 올리면 찍혀요", "scenery_photos", 10),
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
        # 여행에 올린 사진(travel_image) 전부를 센다 — 릴스와 같이 today를 안 넘긴다.
        StampType.SCENERY_PHOTOS: stamp_dao.count_travel_photos(db, user_idx),
        StampType.FIVE_REELS: stamp_dao.count_my_reels(db, user_idx),
        StampType.FOUR_SEASONS: len(_seasons(stamp_dao.finished_travel_periods(db, user_idx, today))),
    }


_TITLE_OF = {stamp_type: title for stamp_type, title, _, _, _ in _STAMPS}
_GOAL_OF = {stamp_type: goal for stamp_type, _, _, _, goal in _STAMPS}


def evaluate(db: Session, user_idx: int) -> list[StampType]:
    """조건을 다시 세서 새로 달성한 스탬프를 적립하고, **이번에 처음 찍힌 것들**을 반환한다.

    **적립과 알림 이력을 한 트랜잭션에 묶는다.** 적립을 먼저 커밋해 버리면 이력을 남기다
    실패했을 때 '이미 딴 스탬프'로 판정돼 알림이 영영 안 나간다(적립만 남고 0통). 그래서
    적립은 flush만 하고, 이력을 만드는 notify가 둘을 함께 커밋한다 — 이력이 실패하면
    notify가 롤백하면서 적립도 같이 되돌아가 다음 재판정에서 다시 시도된다.

    경합(자정 배치 vs 탭 조회 동시)은 유니크 제약이 flush에서 가른다. 진 쪽은 알림을
    보내기 전에 IntegrityError를 받으므로 중복 발송이 없다.

    **이 함수는 트랜잭션을 소유한다** — push_service.notify와 마찬가지로 커밋한다.
    다른 서비스의 트랜잭션 도중에 부르지 마라(그 변경까지 함께 확정된다).

    FCM 발송 자체가 실패하는 건 여기서 손대지 않는다. 이력은 이미 커밋돼 알림 화면에는
    남고 푸시만 못 받는데, 그게 이 저장소의 기존 규칙(at-most-once)이다.
    """
    return _award_reached(db, user_idx, _progress(db, user_idx, now_kst().date()))


def _award_reached(
    db: Session, user_idx: int, progress: dict[StampType, int],
) -> list[StampType]:
    """이미 센 진행도를 받아 적립·알림만 한다. 집계를 두 번 돌지 않으려고 분리했다."""
    already = user_stamp_dao.types_of(db, user_idx)

    earned: list[StampType] = []
    for stamp_type, goal in _GOAL_OF.items():
        if stamp_type.value in already or progress[stamp_type] < goal:
            continue
        try:
            user_stamp_dao.award(db, user_idx, stamp_type.value)  # flush만 — 커밋은 아래에서
        except IntegrityError:
            db.rollback()  # 다른 경로가 먼저 찍었다 — 알림도 그쪽 몫
            continue

        if push_service.is_enabled(db, user_idx, NotificationType.STAMP_EARNED):
            # notify가 이력을 만들고 커밋한다 — 적립도 같은 트랜잭션이라 함께 확정된다.
            if not push_service.notify_stamp_earned(
                db, user_idx, stamp_type.value, _TITLE_OF[stamp_type],
            ):
                continue  # notify가 롤백했다 = 적립도 취소됐다. 다음 재판정에서 재시도.
        else:
            # 수신 거부라 알림은 없지만 스탬프는 딴 것이다 — 적립만 확정한다.
            db.commit()
        earned.append(stamp_type)

    if earned:
        logger.info("스탬프 획득 user=%s: %s", user_idx, [s.value for s in earned])
    return earned


def list_stamps(db: Session, user_idx: int) -> StampListResponse:
    """마이페이지 스탬프 탭 — 9칸 전부와 달성 개수를 함께 내려준다.

    미달성 칸도 빼지 않고 담는다. 화면이 잠긴 칸까지 그리드로 보여주고, 앱이 그 위에
    자물쇠 아이콘을 덮기 때문이다.

    조회하는 김에 재판정도 한다 — 자정 배치가 놓친 사용자(서버가 꺼져 있었다든지)도
    탭을 열면 그 자리에서 찍히고 알림도 나간다. 집계는 한 번만 돌려 재판정과 응답이
    같은 결과를 쓴다(적립이 진행도를 바꾸지는 않는다).
    """
    progress = _progress(db, user_idx, now_kst().date())
    _award_reached(db, user_idx, progress)
    achieved_at = user_stamp_dao.achieved_at_map(db, user_idx)

    items = []
    for stamp_type, title, description, slug, goal in _STAMPS:
        earned_at = achieved_at.get(stamp_type.value)
        items.append(StampItem(
            type=stamp_type,
            title=title,
            description=description,
            image_url=f"{_ICON_BASE}/{slug}.png",
            achieved=earned_at is not None,
            # 이미 딴 칸은 목표치로 채워 보여준다 — 나중에 여행을 지워 실제 카운트가
            # 줄어도 '딴 스탬프가 8/10으로 보이는' 이상한 화면이 되지 않게.
            progress=goal if earned_at else min(progress[stamp_type], goal),
            goal=goal,
            achieved_at=earned_at,
        ))
    return StampListResponse(
        achieved_count=sum(1 for item in items if item.achieved),
        total_count=len(items),
        stamps=items,
    )


# 시작 보정이 거슬러 올라가는 날짜 폭. 서버가 며칠 꺼져 있었어도 그 사이 끝난 여행을
# 잡으려면 어제 하루만으로는 부족하다. 이미 찍힌 스탬프는 적립에서 걸러지므로 넓게 봐도
# 중복 알림은 없고, 비용은 '그 기간에 여행이 끝난 사용자 수'에만 비례한다.
CATCHUP_DAYS = 30


def award_for_finished_travels(lookback_days: int = 1) -> int:
    """최근 lookback_days일 사이에 여행이 끝난 사용자들을 재판정한다. 새로 찍힌 수를 반환.

    배치 컨텍스트라 요청 스코프가 아닌 자체 세션을 연다(trip_reminder_service와 같은 성격).
    스탬프 대부분은 '다녀왔을 때' 켜지는데 그건 날짜가 지나서 일어나는 일이라, 이렇게
    시간으로 훑지 않으면 사용자가 앱을 열기 전까지 알림이 나가지 않는다.

    매 자정 실행은 기본값(어제 하루)이면 충분하지만, **서버 시작 시 보정은 CATCHUP_DAYS로
    넓게 부른다** — 이틀 넘게 꺼져 있었다면 어제만 봐서는 그 사이 끝난 여행을 영영 놓친다.

    여러 번 돌아도 안전하다 — 이미 찍힌 스탬프는 적립에서 걸러진다.
    """
    from databases.database import SessionLocal
    from datetime import timedelta

    end = now_kst().date() - timedelta(days=1)
    start = end - timedelta(days=max(0, lookback_days - 1))
    db = SessionLocal()
    try:
        total = 0
        for user_idx in stamp_dao.user_idxs_with_trip_ending_between(db, start, end):
            try:
                total += len(evaluate(db, user_idx))
            except Exception as e:
                # 한 사람 때문에 나머지가 밀리면 안 된다.
                logger.warning("스탬프 재판정 실패 user=%s: %s", user_idx, e)
                db.rollback()
        return total
    finally:
        db.close()


def award_after_reels(user_idx: int) -> None:
    """릴스 렌더가 끝난 직후 재판정 — '여행 영상 5개'만은 날짜가 아니라 행동으로 켜진다.

    렌더 스레드에서 불리므로 자체 세션을 열고, 어떤 예외도 밖으로 내보내지 않는다.
    스탬프 때문에 영상 발행이 실패하면 안 된다.
    """
    from databases.database import SessionLocal

    db = SessionLocal()
    try:
        evaluate(db, user_idx)
    except Exception as e:
        logger.warning("릴스 후 스탬프 재판정 실패 user=%s: %s", user_idx, e)
    finally:
        db.close()
