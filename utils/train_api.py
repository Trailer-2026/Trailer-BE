"""TAGO 열차정보 API(GetStrtpntAlocFndTrainInfo) 클라이언트.

출발역(NAT)·도착역(NAT)·날짜 1콜로 그 구간 열차 전체(등급·시각·요금)를 받는다.
(dep, arr, date) 단위 lru_cache — 같은 검색 재요청 시 API 콜 0.
코레일 데이터지만 응답에 SRT도 일부 포함된다.
"""
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from config import Config

_BASE = "https://apis.data.go.kr/1613000/TrainInfo/GetStrtpntAlocFndTrainInfo"
_DT_FMT = "%Y%m%d%H%M%S"  # "20260703051300"
# 열차 시각은 전부 한국 표준시(KST). 한국은 DST가 없어 고정 +09:00으로 둔다.
# 파싱 시점에 붙여 출력 JSON이 '+09:00'을 달고 나가도록(naive라 프론트가 UTC로 오해하는 것 방지).
KST = timezone(timedelta(hours=9))


@lru_cache(maxsize=512)
def fetch_trains(dep_nat: str, arr_nat: str, ymd: str) -> tuple:
    """ymd(YYYYMMDD)에 dep_nat→arr_nat 운행 열차 전체. dep_time 순 정렬.

    반환: ({train_no, grade, dep_station, arr_station, dep_time, arr_time, fare}, ...)
    네트워크/파싱 실패 시 예외를 그대로 올린다(서비스에서 502로 변환).
    """
    params = {
        "serviceKey": Config.read("traininfo", "service_key"),
        "pageNo": 1,
        "numOfRows": 500,  # 한 구간 하루 최대 ~수십 편 << 500
        "_type": "json",
        "depPlaceId": dep_nat,
        "arrPlaceId": arr_nat,
        "depPlandTime": ymd,
    }
    url = _BASE + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as r:
        body = json.load(r)["response"]["body"]

    items = body.get("items") or ""
    if not items:  # 0건이면 "" 로 옴 (data.go.kr 특성)
        return ()
    rows = items["item"]
    if isinstance(rows, dict):  # 1건이면 list 아니라 dict
        rows = [rows]

    out = [
        {
            "train_no": x["trainno"],
            "grade": x["traingradename"],
            "dep_station": x["depplacename"],
            "arr_station": x["arrplacename"],
            "dep_time": datetime.strptime(x["depplandtime"], _DT_FMT).replace(tzinfo=KST),
            "arr_time": datetime.strptime(x["arrplandtime"], _DT_FMT).replace(tzinfo=KST),
            # 운임: API가 주는 adultcharge(어른 1인 편도 요금, 원)를 그대로 사용.
            # 좌석 등급(특실)·할인 구분 없고, 일부 열차는 값이 없어 None으로 둔다.
            "fare": int(x["adultcharge"]) if x.get("adultcharge") else None,
        }
        for x in rows
    ]
    out.sort(key=lambda t: t["dep_time"])
    return tuple(out)
