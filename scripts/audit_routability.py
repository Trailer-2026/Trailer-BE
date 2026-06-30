"""전 역 운행 가능 여부 1회 감사.

각 역이 주요 거점(다양한 노선 대표역)과 직통 열차가 1편이라도 있는지 확인한다.
모든 거점과 양방향 0편이면 'DEAD'(TAGO API에 운행 데이터 없음 → picker에서 제외 후보).
호출 오류로 확인 불가하면 'UNKNOWN'(안전하게 보존).

실행: PYTHONIOENCODING=utf-8 python scripts/audit_routability.py [YYYYMMDD]
"""
import json
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from config import Config
from databases.database import SessionLocal
from databases.models.station import Station

_BASE = "http://apis.data.go.kr/1613000/TrainInfo/GetStrtpntAlocFndTrainInfo"
_KEY = Config.read("traininfo", "service_key")
YMD = sys.argv[1] if len(sys.argv) > 1 else "20260703"

# 노선 다양성 커버: 경부/호남/전라/중앙/영동/동해/경전/중부내륙/경북
TARGET_NAMES = [
    "서울역", "청량리역", "대전역", "동대구역", "부산역", "익산역",
    "순천역", "영주역", "충주역", "진주역", "태화강역", "광주송정역",
]


def _count(dep_nat: str, arr_nat: str) -> int:
    """직통 편수(totalCount). 오류 시 -1."""
    params = {
        "serviceKey": _KEY, "pageNo": 1, "numOfRows": 1, "_type": "json",
        "depPlaceId": dep_nat, "arrPlaceId": arr_nat, "depPlandTime": YMD,
    }
    url = _BASE + "?" + urllib.parse.urlencode(params)
    for _ in range(2):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return int(json.load(r)["response"]["body"].get("totalCount", 0))
        except Exception:
            continue
    return -1


def classify(station, targets):
    errored = False
    for t in targets:
        if t.nat_code == station.nat_code:
            continue
        a = _count(station.nat_code, t.nat_code)
        if a > 0:
            return station, "OK"
        b = _count(t.nat_code, station.nat_code)
        if b > 0:
            return station, "OK"
        if a < 0 or b < 0:
            errored = True
    return station, ("UNKNOWN" if errored else "DEAD")


def main():
    db = SessionLocal()
    stations = db.query(Station).filter(
        Station.deleted_at.is_(None), Station.nat_code.isnot(None)
    ).all()
    by_name = {s.station_name: s for s in stations}
    targets = [by_name[n] for n in TARGET_NAMES if n in by_name]
    print(f"감사 시작: {len(stations)}역 × 거점 {len(targets)}곳, 날짜 {YMD}")

    results = {"OK": [], "DEAD": [], "UNKNOWN": []}
    with ThreadPoolExecutor(max_workers=24) as ex:
        for s, verdict in ex.map(lambda st: classify(st, targets), stations):
            results[verdict].append(s.station_name)

    print(f"\nOK {len(results['OK'])} | DEAD {len(results['DEAD'])} | UNKNOWN {len(results['UNKNOWN'])}")
    print("\n=== DEAD (운행 0, 제외 후보) ===")
    for n in sorted(results["DEAD"]):
        print("  ", n)
    if results["UNKNOWN"]:
        print("\n=== UNKNOWN (오류로 미확정, 보존) ===")
        for n in sorted(results["UNKNOWN"]):
            print("  ", n)
    db.close()


if __name__ == "__main__":
    main()
