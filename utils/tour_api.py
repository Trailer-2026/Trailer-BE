"""한국관광공사 국문 관광정보 서비스(TourAPI) 클라이언트.

data.go.kr 15101578 (KorService1). 추천지(place) 마스터 시딩의 데이터 소스.
serviceKey는 config [tourapi] service_key (Decoding 키)에서 읽는다.

train_api.py와 동형: urllib + _type=json, 실패 시 예외를 그대로 올린다.
"""
import json
import urllib.parse
import urllib.request

from config import Config

_BASE = "http://apis.data.go.kr/B551011/KorService2"
_COMMON = {
    "MobileOS": "ETC",
    "MobileApp": "Trailer",
    "_type": "json",
}


def _service_key() -> str:
    # Decoding 키. urlencode가 다시 인코딩하므로 원본(디코딩) 값을 넣는다.
    return Config.read("tourapi", "service_key")


def _get(operation: str, params: dict, timeout: int = 20) -> dict:
    """KorService2 오퍼레이션 1콜. response.body(dict)를 반환한다."""
    q = {**_COMMON, "serviceKey": _service_key(), **params}
    url = f"{_BASE}/{operation}?" + urllib.parse.urlencode(q)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        payload = json.load(r)
    # data.go.kr 오류는 두 형태: (1) 최상위 {resultCode, resultMsg} (2) response.header.resultCode
    if "response" not in payload:
        raise RuntimeError(f"TourAPI {operation} 실패: {payload.get('resultCode')} {payload.get('resultMsg')}")
    resp = payload["response"]
    header = resp.get("header") or {}
    if header.get("resultCode") not in ("0000", "0", None):
        raise RuntimeError(f"TourAPI {operation} 실패: {header.get('resultCode')} {header.get('resultMsg')}")
    return resp.get("body") or {}


def _items(body: dict) -> list[dict]:
    """body.items.item 을 항상 list로 정규화한다(0건이면 [], 1건이면 [dict])."""
    items = body.get("items")
    if not items:  # 0건이면 "" 로 옴 (data.go.kr 특성)
        return []
    item = items.get("item")
    if item is None:
        return []
    return item if isinstance(item, list) else [item]


def area_based_list(
    *,
    area_code: int | None = None,
    sigungu_code: int | None = None,
    content_type_id: int | None = None,
    cat1: str | None = None,
    cat2: str | None = None,
    cat3: str | None = None,
    page_no: int = 1,
    num_of_rows: int = 100,
    arrange: str = "O",  # O: 대표이미지 있는 제목순(이미지·좌표 보장 프록시)
) -> tuple[list[dict], int]:
    """지역기반 관광정보 조회(areaBasedList2). (items, totalCount) 반환.

    item 주요 필드: contentid, contenttypeid, title, addr1, areacode, sigungucode,
    cat1/cat2/cat3, mapx(경도), mapy(위도), firstimage.
    """
    params = {"numOfRows": num_of_rows, "pageNo": page_no, "arrange": arrange}
    if area_code is not None:
        params["areaCode"] = area_code
    if sigungu_code is not None:
        params["sigunguCode"] = sigungu_code
    if content_type_id is not None:
        params["contentTypeId"] = content_type_id
    if cat1:
        params["cat1"] = cat1
    if cat2:
        params["cat2"] = cat2
    if cat3:
        params["cat3"] = cat3
    body = _get("areaBasedList2", params)
    return _items(body), int(body.get("totalCount") or 0)


def location_based_list(
    *,
    lat: float,
    lng: float,
    radius_m: int = 20000,  # locationBasedList2 최대 20km
    content_type_id: int | None = None,
    num_of_rows: int = 100,
    page_no: int = 1,
    arrange: str = "E",  # E: 거리순(가까운 순)
) -> tuple[list[dict], int]:
    """위치기반 관광정보 조회(locationBasedList2). (items, totalCount) 반환.

    item에 dist(중심으로부터 거리 m)가 추가로 들어온다. mapX=경도, mapY=위도.
    """
    params = {
        "numOfRows": num_of_rows, "pageNo": page_no, "arrange": arrange,
        "mapX": lng, "mapY": lat, "radius": radius_m,
    }
    if content_type_id is not None:
        params["contentTypeId"] = content_type_id
    body = _get("locationBasedList2", params)
    return _items(body), int(body.get("totalCount") or 0)


def category_code(
    *,
    content_type_id: int | None = None,
    cat1: str | None = None,
    cat2: str | None = None,
) -> list[dict]:
    """서비스 분류코드 조회(categoryCode2). item: {code, name, rnum}.

    cat1/cat2를 주면 그 하위 코드 목록을 반환한다(대→중→소). 코드→한글명 매핑 구축용.
    """
    params = {"numOfRows": 100, "pageNo": 1}
    if content_type_id is not None:
        params["contentTypeId"] = content_type_id
    if cat1:
        params["cat1"] = cat1
    if cat2:
        params["cat2"] = cat2
    body = _get("categoryCode2", params)
    return _items(body)


def area_code(area_code: int | None = None) -> list[dict]:
    """지역코드 조회(areaCode2). area_code 미지정 시 시도 목록, 지정 시 시군구 목록."""
    params = {"numOfRows": 100, "pageNo": 1}
    if area_code is not None:
        params["areaCode"] = area_code
    return _items(_get("areaCode2", params))
