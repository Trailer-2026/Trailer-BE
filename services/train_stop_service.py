"""열차별 정차역(train_stop) 갱신 서비스 — 배치/스케줄러 공용.

운행정보 API(travelerTrainRunInfo2)는 필터 조회 불가·과거 한정이라 '최근 하루치'를 받아
train_stop 테이블을 전량 교체한다(정차 패턴은 열차번호별로 안정적이라 하루치면 충분).
scripts/sync_train_stops.py(수동)와 main.py의 일일 자동 갱신 루프가 이 함수를 공유한다.

배치 컨텍스트라 요청 스코프가 아닌 자체 세션을 열고 직접 커밋한다.
"""
import logging
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


def _yesterday_ymd() -> str:
    return (datetime.now(_KST) - timedelta(days=1)).strftime("%Y%m%d")


def _ensure_table() -> None:
    # 참조 데이터 테이블이라 마이그레이션 없이 자체 provision. 서버·standalone 스크립트 양쪽에서
    # 호출되므로 startup이 아닌 여기 둔다(스크립트는 lifespan을 안 타 fresh DB에서 테이블이 필요).
    TrainStop.__table__.create(bind=engine, checkfirst=True)


def refresh(ymd: str | None = None) -> int:
    """대상일(기본 어제) 정차역을 받아 train_stop을 전량 교체 적재하고 적재 행수를 반환.

    빈 응답(그 날짜 미제공 등)이면 기존 데이터를 지우지 않고 0을 반환한다(good data 보존).
    """
    ymd = ymd or _yesterday_ymd()
    _ensure_table()
    records = train_stops.fetch_day(ymd)
    if not records:
        logger.warning("train_stop: %s 정차역 0건 — 기존 데이터 유지, 갱신 건너뜀", ymd)
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
_cache: tuple[datetime | None, frozenset[tuple[str, str]]] | None = None


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


def direct_links() -> frozenset[tuple[str, str]]:
    """직통 연결 (앞역, 뒷역) 순서쌍 집합. 역명은 train_stop 형식('역' 접미사 없음).

    데이터가 없으면 빈 집합 — 호출부는 그 때 '판정 불가'로 보고 평소대로 조회해야 한다.
    **어떤 이유로든 실패해도 빈 집합**이다(테이블 미생성·DB 장애 등). 이 인덱스는 조회를
    줄이는 최적화일 뿐이라, 못 만들었다고 추천 자체가 죽으면 손해가 훨씬 크다.
    """
    global _cache
    db = SessionLocal()
    try:
        version = train_stop_dao.latest_created_at(db)
        cached = _cache  # 이름 하나만 읽어 버전·내용이 어긋나지 않게 한다
        if cached is not None and cached[0] == version:
            return cached[1]
        rows = train_stop_dao.all_sequences(db)
    except Exception as e:
        logger.warning("train_stop 직통 인덱스 조회 실패(프리페치 필터 비활성): %s", e)
        return frozenset()
    finally:
        db.close()
    links = _build_links(rows)
    _cache = (version, links)
    logger.info("train_stop 직통 인덱스 구축: %d쌍", len(links))
    return links


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
