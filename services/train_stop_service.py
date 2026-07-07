"""열차별 정차역(train_stop) 갱신 서비스 — 배치/스케줄러 공용.

운행정보 API(travelerTrainRunInfo2)는 필터 조회 불가·과거 한정이라 '최근 하루치'를 받아
train_stop 테이블을 전량 교체한다(정차 패턴은 열차번호별로 안정적이라 하루치면 충분).
scripts/sync_train_stops.py(수동)와 main.py의 일일 자동 갱신 루프가 이 함수를 공유한다.

배치 컨텍스트라 요청 스코프가 아닌 자체 세션을 열고 직접 커밋한다.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

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


def refresh(ymd: str | None = None) -> int:
    """대상일(기본 어제) 정차역을 받아 train_stop을 전량 교체 적재하고 적재 행수를 반환.

    빈 응답(그 날짜 미제공 등)이면 기존 데이터를 지우지 않고 0을 반환한다(good data 보존).
    """
    ymd = ymd or _yesterday_ymd()
    TrainStop.__table__.create(bind=engine, checkfirst=True)  # 없으면 생성(참조 데이터)
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


def refresh_if_stale(max_age_hours: int = _FRESH_WITHIN_HOURS) -> int | None:
    """최근 갱신이 없거나 오래됐을 때만 refresh(). 최신이면 None(생략).

    서버 시작 시 1회 호출용 — 데이터가 비었거나 하루 지났으면 즉시 채우고, 방금 갱신됐으면 건너뛴다.
    """
    TrainStop.__table__.create(bind=engine, checkfirst=True)
    db = SessionLocal()
    try:
        latest = db.query(func.max(TrainStop.created_at)).scalar()
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
