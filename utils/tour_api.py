"""한국관광공사 국문 관광정보 서비스(TourAPI) 클라이언트.

data.go.kr 15101578 (KorService1). 추천지(place) 마스터 시딩의 데이터 소스.
serviceKey는 config [tourapi] service_key (Decoding 키)에서 읽는다.

train_api.py와 동형: urllib + _type=json, 실패 시 예외를 그대로 올린다.
"""
import json
import urllib.parse

from utils import dgo

_BASE = "https://apis.data.go.kr/B551011/KorService2"
_COMMON = {
    "MobileOS": "ETC",
    "MobileApp": "Trailer",
    "_type": "json",
}
# serviceKey는 dgo가 주입한다(config [tourapi] service_key, 콤마로 여러 키 가능).


def _get(operation: str, params: dict, timeout: int = 20) -> dict:
    """KorService2 오퍼레이션 1콜. response.body(dict)를 반환한다.

    serviceKey 주입·거부 시 다음 키로의 로테이션은 dgo가 맡는다(TAGO와 같은 계정 키를 쓰므로
    죽은 키 정보를 공유한다). body만 필요한 dgo.get_body와 달리 여기선 header.resultCode까지
    봐야 해서 요청 자체는 직접 만든다.

    **죽은 키는 오퍼레이션 단위로 센다**(scope에 operation까지 넣는 이유). data.go.kr의 일일
    쿼터가 오퍼레이션별이라(응답 헤더 X-RateLimit-Limit=1000) detailIntro2가 소진돼도 같은 키의
    areaBasedList2는 멀쩡하다(실측 확인). KorService2 하나로 묶으면 운영시간 조회가 쿼터를 다
    쓴 순간 관광지 목록 조회까지 같이 막혀 **추천 코스의 장소가 통째로 0건**이 된다.
    """
    def _call(key: str) -> dict:
        q = {**_COMMON, "serviceKey": key, **params}
        url = f"{_BASE}/{operation}?" + urllib.parse.urlencode(q)
        payload = dgo.fetch_json(url, timeout)
        # data.go.kr 오류는 두 형태: (1) 최상위 {resultCode, resultMsg} (2) response.header.resultCode
        if "response" not in payload:
            dgo.raise_if_key_problem(json.dumps(payload, ensure_ascii=False))
            raise RuntimeError(f"TourAPI {operation} 실패: {payload.get('resultCode')} {payload.get('resultMsg')}")
        resp = payload["response"]
        header = resp.get("header") or {}
        if header.get("resultCode") not in ("0000", "0", None):
            dgo.raise_if_key_problem(json.dumps(header, ensure_ascii=False))
            raise RuntimeError(f"TourAPI {operation} 실패: {header.get('resultCode')} {header.get('resultMsg')}")
        return resp.get("body") or {}

    return dgo.with_key(_call, f"{_BASE}/{operation}")


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
    return dgo.items(body), int(body.get("totalCount") or 0)


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
    return dgo.items(body), int(body.get("totalCount") or 0)


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
    items = dgo.items(body)
    return items[0] if items else {}
