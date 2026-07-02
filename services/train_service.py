import logging
from datetime import datetime

from sqlalchemy.orm import Session

from core.exceptions.custom import (
    BadRequestException,
    ExternalServiceException,
    NotFoundException,
)
from databases.daos import station_dao
from schemas.train_schema import TrainSearchResponse
from utils import train_api

logger = logging.getLogger(__name__)

# 내일로패스 미적용(제외) 등급 prefix: KTX 계열 전체 + SRT.
# 나머지(무궁화호·누리로·ITX-새마을·ITX-마음·ITX-청춘)는 패스 적용 대상.
# "KTX-산천(A-type)" 등 변형까지 잡으려고 정확매칭 대신 prefix로 거른다.
_NAIL_EXCLUDED_PREFIX = ("KTX", "SRT")


def _nail_eligible(grade: str) -> bool:
    return not grade.startswith(_NAIL_EXCLUDED_PREFIX)


def _nat_code(db: Session, station_idx: int, label: str) -> str:
    """station_idx → NAT 코드. 역이 없으면 404, 시간표 미지원 역이면 400."""
    station = station_dao.get_by_idx(db, station_idx)
    if not station:
        raise NotFoundException(f"{label} 역을 찾을 수 없습니다.")
    if not station.nat_code:
        raise BadRequestException(f"{station.station_name}은(는) 시간표 조회를 지원하지 않습니다.")
    return station.nat_code


def search_trains(
    db: Session,
    dep_idx: int,
    arr_idx: int,
    date: str,
    time: str | None = None,
    nail_pass: bool = False,
) -> list[TrainSearchResponse]:
    """dep_idx→arr_idx 구간의 date(YYYYMMDD) 운행 열차를 조회한다.

    time(HH:MM)을 주면 그 시각 이후 출발 열차만 반환한다. 결과 없음은 빈 배열.
    nail_pass=True면 내일로패스 적용 열차(KTX 계열·SRT 제외)만 반환한다.
    외부 열차정보 API 실패는 502로 변환한다.
    """
    try:
        datetime.strptime(date, "%Y%m%d")
    except ValueError:
        raise BadRequestException("date는 YYYYMMDD 형식이어야 합니다.")

    after = None
    if time:
        try:
            after = datetime.strptime(time, "%H:%M").time()
        except ValueError:
            raise BadRequestException("time은 HH:MM 형식이어야 합니다.")

    dep_nat = _nat_code(db, dep_idx, "출발")
    arr_nat = _nat_code(db, arr_idx, "도착")

    try:
        rows = train_api.fetch_trains(dep_nat, arr_nat, date)
    except Exception as e:
        logger.warning("열차정보 API 호출 실패: %s", e)
        raise ExternalServiceException("열차 시간표 조회에 실패했습니다.")

    result = []
    for r in rows:
        if after and r["dep_time"].time() < after:
            continue
        if nail_pass and not _nail_eligible(r["grade"]):
            continue
        duration = int((r["arr_time"] - r["dep_time"]).total_seconds() // 60)
        result.append(
            TrainSearchResponse(
                train_no=r["train_no"],
                grade=r["grade"],
                dep_station=r["dep_station"],
                arr_station=r["arr_station"],
                dep_time=r["dep_time"],
                arr_time=r["arr_time"],
                duration_minutes=duration,
                fare=r["fare"],
            )
        )
    return result
