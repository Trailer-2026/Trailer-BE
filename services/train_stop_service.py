"""열차별 정차역(train_stop) 갱신 서비스 — 배치/스케줄러 공용.

운행정보 API(travelerTrainRunInfo2)는 필터 조회 불가·과거 한정이라 '최근 하루치'를 받아
train_stop 테이블을 전량 교체한다(정차 패턴은 열차번호별로 안정적이라 하루치면 충분).
scripts/sync_train_stops.py(수동)와 main.py의 일일 자동 갱신 루프가 이 함수를 공유한다.

배치 컨텍스트라 요청 스코프가 아닌 자체 세션을 열고 직접 커밋한다.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from databases.daos import train_stop_dao
from databases.database import SessionLocal, engine
from databases.models.train_stop import TrainStop
from utils import train_stops

logger = logging.getLogger(__name__)

# 자동 갱신 주기(초) — 하루 한 번.
REFRESH_INTERVAL_SEC = 24 * 60 * 60
# 이 시간(h) 안에 갱신된 데이터가 있으면 시작 시 재요청 생략(개발 --reload 재시작마다 API 호출 방지).
_FRESH_WITHIN_HOURS = 20
# KST(운행일자 기준). 어제가 운행정보 보존기간(3개월~1일 전)의 최신.
_KST = timezone(timedelta(hours=9))


# 한 번 갱신할 때 훑는 날짜 수(어제부터 거슬러). 하루치만 받으면 그날 안 다닌 열차가
# 빠져 '정차역 N개'와 창밖 풍경이 조용히 비는데, 정차 패턴은 열차번호별로 안정적이라
# 날짜를 넓혀도 결과가 나빠지지 않는다. 대가는 조회 호출이 날짜 수만큼 느는 것뿐이다.
_REFRESH_DAYS = 3


def _yesterday_ymd() -> str:
    return (datetime.now(_KST) - timedelta(days=1)).strftime("%Y%m%d")


def _ensure_table() -> None:
    # 참조 데이터 테이블이라 마이그레이션 없이 자체 provision. 서버·standalone 스크립트 양쪽에서
    # 호출되므로 startup이 아닌 여기 둔다(스크립트는 lifespan을 안 타 fresh DB에서 테이블이 필요).
    TrainStop.__table__.create(bind=engine, checkfirst=True)


def refresh(ymd: str | None = None, days: int = _REFRESH_DAYS) -> int:
    """최근 며칠치 정차역을 합쳐 train_stop을 전량 교체 적재하고 적재 행수를 반환.

    **하루치만 받으면 그날 안 다닌 열차가 통째로 빠진다.** 주말·평일에만 다니는 편성,
    격일 임시편이 그렇고, 그런 열차를 태운 여정은 '정차역 N개'가 비고 창밖 풍경 구간도
    잡히지 않는다(추천 결과에 '개 역 이동'이 숫자 없이 뜨던 것이 이 경우다).

    열차 하나의 정차 순서는 **한 날짜에서 통째로** 가져온다 — 날짜별 조각을 섞으면 같은
    열차번호에 서로 다른 노선이 겹쳐 순서가 무너진다. 최근 날짜부터 훑어 그 열차가 처음
    나온 날의 것만 취한다.

    빈 응답(그 날짜 미제공 등)이면 기존 데이터를 지우지 않고 0을 반환한다(good data 보존).
    """
    _ensure_table()
    base = datetime.strptime(ymd, "%Y%m%d") if ymd else datetime.now(_KST) - timedelta(days=1)
    records: list[dict] = []
    collected: set[str] = set()
    for offset in range(max(1, days)):
        target = (base - timedelta(days=offset)).strftime("%Y%m%d")
        try:
            day_records = train_stops.fetch_day(target)
        except Exception as e:  # 하루가 실패해도 나머지 날짜로 계속 간다
            logger.warning("train_stop: %s 조회 실패(%s) — 건너뜀", target, type(e).__name__)
            continue
        fresh = {r["trn_no"] for r in day_records} - collected
        if not fresh:
            continue
        records += [r for r in day_records if r["trn_no"] in fresh]
        collected |= fresh
        logger.info("train_stop: %s 에서 열차 %d편 추가 (누적 %d편)", target, len(fresh), len(collected))

    if not records:
        logger.warning("train_stop: 최근 %d일 정차역 0건 — 기존 데이터 유지, 갱신 건너뜀", days)
        return 0
    db = SessionLocal()
    try:
        n = train_stop_dao.replace_all(db, records)
        db.commit()
        return n
    finally:
        db.close()


# ── 직통 연결 인덱스 (열차 시간표 API 헛호출 제거용) ──────────────────────────
# 한 열차가 A를 지나 B에 서면 A→B 직통이 존재한다. 그 (A,B) 순서쌍 전체를 미리 펼쳐 둔다.
# 왜 필요한가: 추천 1회가 쏘는 열차 조회 100~190콜 중 **70%가 "열차 0편"** 응답이었다
# (실측, 서울→부산 192콜 중 135콜). 있지도 않은 직통을 물어보는 것이라 DB로 걸러낼 수 있다.
# 갱신은 하루 1회 전량 교체라 적재 시각(created_at 최댓값)이 바뀔 때만 인덱스를 다시 만든다.
# (적재 시각, 순서쌍 집합) 한 덩어리로 둔다 — 이름 하나에 대입하는 건 GIL이 원자성을 보장하므로
# 락 없이도 버전과 내용이 어긋난 채로 읽히지 않는다. 동시에 두 번 만들어도 결과는 같다.
# None = 아직 안 만듦. 빈 집합도 '만든 결과'라 그대로 재사용해야 한다(테이블이 비었을 때
# 매 호출 전량 조회하는 걸 막는다) — 그래서 빈 집합 falsy가 아니라 None으로 미구축을 판별한다.
# 튜플 = (버전 확인 시각 monotonic, 적재 시각, 순서쌍, 인덱스에 등장하는 역).
_cache: tuple[float, datetime | None, frozenset[tuple[str, str]], frozenset[str]] | None = None
# 적재 시각을 DB에 다시 묻는 최소 간격(초). 환승 탐색이 구간마다 no_direct를 부르는데(추천 1회에
# 수백 번) 매번 물으면 그만큼 DB 왕복이 붙는다 — 운영 VM(us-central1)에서 Cloud SQL(서울)까지
# 왕복이 0.18초다. 갱신은 하루 1회라 1분 늦게 알아채도 잃는 게 없다.
_VERSION_CHECK_SEC = 60
# 인덱스를 한 번도 못 만든 채 실패했을 때의 버전 표식. None은 '빈 테이블'이라는 진짜 버전이라 못 쓴다.
_FAILED = object()


def _build_links(rows: list[tuple[str, int, str]]) -> frozenset[tuple[str, str]]:
    """(trn_no, seq, stn_nm) 목록 → 한 열차로 이어지는 (앞역, 뒷역) 순서쌍 집합."""
    by_train: dict[str, list[str]] = {}
    for trn_no, _seq, stn_nm in rows:  # DAO가 seq 오름차순으로 준다
        by_train.setdefault(trn_no, []).append(stn_nm)
    out = set()
    for stops in by_train.values():
        for i, a in enumerate(stops):
            for b in stops[i + 1:]:
                out.add((a, b))
    return frozenset(out)


def direct_links() -> tuple[frozenset[tuple[str, str]], frozenset[str]]:
    """(직통 연결 (앞역, 뒷역) 순서쌍 집합, 인덱스에 등장하는 역 집합). 역명은 train_stop 형식('역' 접미사 없음).

    데이터가 없으면 빈 집합 — 호출부는 그 때 '판정 불가'로 보고 평소대로 조회해야 한다.
    **어떤 이유로든 실패해도 예외를 올리지 않는다**(테이블 미생성·DB 장애 등). 이 인덱스는 조회를
    줄이는 최적화일 뿐이라, 못 만들었다고 추천 자체가 죽으면 손해가 훨씬 크다.
    **실패도 _VERSION_CHECK_SEC 동안 기억한다** — 직전 인덱스가 있으면 그걸 계속 쓰고, 없으면 빈
    결과를 쓴다. 추천 1회가 수백 번 부르는 함수라, 안 그러면 DB가 잠깐 끊긴 사이 호출마다 연결을
    새로 시도하고 경고를 찍는다(운영 Cloud SQL은 이틀에 한 번꼴로 연결이 끊겼다).
    """
    global _cache
    cached = _cache  # 이름 하나만 읽어 버전·내용이 어긋나지 않게 한다
    if cached is not None and time.monotonic() - cached[0] < _VERSION_CHECK_SEC:
        return cached[2], cached[3]
    db = None  # 세션 생성 자체가 터져도(엔진 설정 오류 등) 아래 except로 떨어지게 try 안에서 연다
    try:
        db = SessionLocal()
        version = train_stop_dao.latest_created_at(db)
        if cached is not None and cached[1] == version:
            _cache = (time.monotonic(), *cached[1:])
            return cached[2], cached[3]
        rows = train_stop_dao.all_sequences(db)
    except Exception as e:
        logger.warning("train_stop 직통 인덱스 조회 실패(%d초간 %s): %s", _VERSION_CHECK_SEC,
                       "직전 인덱스 사용" if cached else "필터 비활성", e)
        kept = (time.monotonic(), *cached[1:]) if cached else (time.monotonic(), _FAILED, frozenset(), frozenset())
        _cache = kept
        return kept[2], kept[3]
    finally:
        if db is not None:
            db.close()
    links = _build_links(rows)
    known = frozenset(n for pair in links for n in pair)
    _cache = (time.monotonic(), version, links, known)
    logger.info("train_stop 직통 인덱스 구축: %d쌍", len(links))
    return links, known


def no_direct(a: str, b: str) -> bool:
    """a→b(train_stop 역명)를 잇는 열차가 **없다고 확인되는가**.

    인덱스가 비었거나 한쪽이라도 train_stop에 없는 역(SRT 동탄·판교, 관광열차 노선 등)이면
    판정 불가라 False다 — 호출부는 평소대로 조회해야 한다.
    **한계**: 스냅샷이 '어제 하루치'라 그날 안 다닌 요일 한정 열차는 모른다(실측: 부산→마산·진주
    금·일 1편이 목요일 스냅샷엔 없다). 그래서 이 판정을 믿어도 되는 곳은 대체 경로가 있는 환승
    거점 다리뿐이다 — 사용자가 고른 구간의 직통 판정에 쓰면 그 1편이 조용히 사라진다.
    """
    links, known = direct_links()
    return a in known and b in known and (a, b) not in links


def refresh_if_stale(max_age_hours: int = _FRESH_WITHIN_HOURS) -> int | None:
    """최근 갱신이 없거나 오래됐을 때만 refresh(). 최신이면 None(생략).

    서버 시작 시 1회 호출용 — 데이터가 비었거나 하루 지났으면 즉시 채우고, 방금 갱신됐으면 건너뛴다.
    """
    _ensure_table()
    db = SessionLocal()
    try:
        latest = train_stop_dao.latest_created_at(db)
    finally:
        db.close()
    if latest is not None:
        # sqlite 폴백·tz 미설정 환경에선 created_at이 naive로 온다 → KST로 간주해 aware로 정규화한 뒤
        # 비교한다(aware now - naive latest는 TypeError). Postgres(timestamptz)는 이미 aware라 무영향.
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=_KST)
        age = datetime.now(_KST) - latest
        if age < timedelta(hours=max_age_hours):
            return None
    return refresh()
