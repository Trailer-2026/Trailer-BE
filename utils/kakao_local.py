"""카카오 로컬 API 클라이언트 — 키워드 장소 검색.

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
