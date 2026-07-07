"""한국관광공사 국문 관광정보 서비스(TourAPI) 클라이언트.

data.go.kr 15101578 (KorService1). 추천지(place) 마스터 시딩의 데이터 소스.
serviceKey는 config [tourapi] service_key (Decoding 키)에서 읽는다.

train_api.py와 동형: urllib + _type=json, 실패 시 예외를 그대로 올린다.
"""
import json
import urllib.parse
import urllib.request

from config import Config

_BASE = "https://apis.data.go.kr/B551011/KorService2"
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
    content_type_id: int | None = None,
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
    if content_type_id is not None:
        params["contentTypeId"] = content_type_id
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


def detail_intro(*, content_id: str, content_type_id: int, timeout: int = 20) -> dict:
    """공통정보 상세 조회(detailIntro2) 1건. 유형별 운영시간·휴무 필드를 담은 item(dict) 반환.

    contentId·contentTypeId가 모두 필요하다. 유형마다 시간/휴무 필드명이 다르다
    (관광지 usetime/restdate, 음식점 opentimefood/restdatefood 등 — utils.tour_place 참조).
    항목이 없으면 빈 dict.
    """
    body = _get(
        "detailIntro2",
        {"contentId": content_id, "contentTypeId": content_type_id},
        timeout=timeout,
    )
    items = _items(body)
    return items[0] if items else {}
