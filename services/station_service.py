import logging

from sqlalchemy.orm import Session

from core.exceptions.custom import BadRequestException
from databases.daos import station_dao
from schemas.station_schema import StationResponse

logger = logging.getLogger(__name__)

# 한글 음절 초성 19자 (유니코드 음절 배열 순서)
_CHOSUNG = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
# 쌍자음 → 대표 자음. ㄱㄴㄷ 인덱스 UI는 대표 자음 14개만 노출하므로 "까"는 "ㄱ"으로 묶는다.
_REP = {"ㄲ": "ㄱ", "ㄸ": "ㄷ", "ㅃ": "ㅂ", "ㅆ": "ㅅ", "ㅉ": "ㅈ"}
# initial 파라미터로 허용하는 대표 자음 14개 (ㄱㄴㄷ 인덱스 버튼)
_VALID_INITIALS = set("ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ")


def _initial_of(name: str) -> str:
    """역명 첫 글자의 대표 초성을 반환한다. 한글 음절이 아니면 첫 글자 그대로."""
    code = ord(name[0]) - 0xAC00
    if 0 <= code < 11172:  # 가(AC00) ~ 힣
        cho = _CHOSUNG[code // 588]
        return _REP.get(cho, cho)
    return name[0]


def search_stations(
    db: Session, query: str | None = None, initial: str | None = None
) -> list[StationResponse]:
    """역 목록을 조회한다(읽기 전용).

    query: 역명 부분일치 검색. initial: 초성(ㄱㄴㄷ) 필터.
    역 수가 적어(약 246역) 초성 계산은 메모리에서 처리한다. 둘 다 주면 AND.
    """
    stations = station_dao.get_stations(db, query)
    if initial:
        rep = _REP.get(initial, initial)
        if rep not in _VALID_INITIALS:
            raise BadRequestException("initial은 자음(ㄱ~ㅎ) 한 글자여야 합니다.")
        stations = [s for s in stations if _initial_of(s.station_name) == rep]
    # 가나다순 정렬: 한글 음절(U+AC00~)은 코드포인트 순서가 곧 가나다순이라
    # DB 컬레이션(괄호 역명이 앞서는 등) 대신 파이썬에서 정렬한다.
    stations.sort(key=lambda s: s.station_name)
    return [StationResponse.model_validate(s) for s in stations]
