"""카카오 로컬 API 클라이언트 — 키워드 장소 검색·좌표 역지오코딩.

직접 일정 만들기의 '장소 추가' 검색창용. TourAPI(관광지 전용)로는 공항·일반 상호를 못 찾아
전국 POI 커버리지가 좋은 카카오 로컬 키워드 검색을 쓴다.

REST 키는 config [kakao] rest_api_key. 카카오 로그인(oauth)은 클라 access_token만 쓰므로
이 서버 REST 키는 별도로 발급받아 넣어야 한다(카카오 developers 앱 REST API 키).
"""
import json
import urllib.parse
import urllib.request

from config import Config

_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
_COORD2REGION_URL = "https://dapi.kakao.com/v2/local/geo/coord2regioncode.json"

# 시/도 정식명 → 릴스 지역 태그에 쓰는 짧은 이름. 규칙으로 줄이면 "경상북도"가
# "경상북"이 돼서 그냥 표로 둔다(17개 시도 + 개편 전 옛 이름 2개).
_SHORT_REGION = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천",
    "광주광역시": "광주", "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종",
    "경기도": "경기", "강원특별자치도": "강원", "강원도": "강원", "충청북도": "충북",
    "충청남도": "충남", "전북특별자치도": "전북", "전라북도": "전북", "전라남도": "전남",
    "경상북도": "경북", "경상남도": "경남", "제주특별자치도": "제주",
}


def _rest_key() -> str:
    return Config.read("kakao", "rest_api_key")


def search_keyword(query: str, size: int = 15, timeout: int = 5) -> list[dict]:
    """키워드로 장소를 검색해 documents(list[dict])를 반환한다.

    document 주요 필드: place_name, x(경도, 문자열), y(위도, 문자열),
    road_address_name/address_name, category_group_name, category_name, phone, place_url.
    HTTP/파싱 실패는 예외를 그대로 올린다(호출부에서 502 변환).
    """
    q = urllib.parse.urlencode({"query": query, "size": size})
    req = urllib.request.Request(
        f"{_URL}?{q}", headers={"Authorization": f"KakaoAK {_rest_key()}"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.load(r)
    return payload.get("documents") or []


def region_of(latitude: float, longitude: float, timeout: int = 5) -> str | None:
    """좌표가 속한 시/도의 짧은 이름("강원", "부산")을 반환한다. 못 찾으면 None.

    릴스 카드의 지역 태그용. 바다·해외 좌표면 documents 가 비어 None 이고,
    표에 없는 이름(행정구역 개편 등)은 정식명을 그대로 돌려준다.
    HTTP/파싱 실패는 예외를 그대로 올린다(호출부에서 판단).
    """
    q = urllib.parse.urlencode({"x": longitude, "y": latitude})
    req = urllib.request.Request(
        f"{_COORD2REGION_URL}?{q}", headers={"Authorization": f"KakaoAK {_rest_key()}"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.load(r)
    for doc in payload.get("documents") or []:
        name = (doc.get("region_1depth_name") or "").strip()
        if name:
            return _SHORT_REGION.get(name, name)
    return None
