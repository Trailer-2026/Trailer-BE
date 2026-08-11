"""카카오 로컬 API 클라이언트 — 키워드 장소 검색·좌표 역지오코딩·최근접 역.

직접 일정 만들기의 '장소 추가' 검색창용. TourAPI(관광지 전용)로는 공항·일반 상호를 못 찾아
전국 POI 커버리지가 좋은 카카오 로컬 키워드 검색을 쓴다.

REST 키는 config [kakao] rest_api_key. 카카오 로그인(oauth)은 클라 access_token만 쓰므로
이 서버 REST 키는 별도로 발급받아 넣어야 한다(카카오 developers 앱 REST API 키).
"""
import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from config import Config

_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
_COORD2REGION_URL = "https://dapi.kakao.com/v2/local/geo/coord2regioncode.json"
_CATEGORY_URL = "https://dapi.kakao.com/v2/local/search/category.json"

# 지하철역 카테고리 그룹 코드. 기차역은 대응하는 그룹 코드가 없어 키워드로 찾는다.
_SUBWAY_GROUP = "SW8"
_TRAIN_QUERY = "기차역"
_SEARCH_SIZE = 15

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


def _documents(url: str, params: dict, timeout: int) -> list[dict]:
    """카카오 로컬 API를 호출해 documents를 반환한다. HTTP/파싱 실패는 예외를 그대로 올린다."""
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{url}?{q}", headers={"Authorization": f"KakaoAK {_rest_key()}"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.load(r)
    return payload.get("documents") or []


def search_keyword(query: str, size: int = 15, timeout: int = 5) -> list[dict]:
    """키워드로 장소를 검색해 documents(list[dict])를 반환한다.

    document 주요 필드: place_name, x(경도, 문자열), y(위도, 문자열),
    road_address_name/address_name, category_group_name, category_name, phone, place_url.
    HTTP/파싱 실패는 예외를 그대로 올린다(호출부에서 502 변환).
    """
    return _documents(_URL, {"query": query, "size": size}, timeout)


def region_of(latitude: float, longitude: float, timeout: int = 5) -> str | None:
    """좌표가 속한 시/도의 짧은 이름("강원", "부산")을 반환한다. 못 찾으면 None.

    릴스 카드의 지역 태그용. 바다·해외 좌표면 documents 가 비어 None 이고,
    표에 없는 이름(행정구역 개편 등)은 정식명을 그대로 돌려준다.
    HTTP/파싱 실패는 예외를 그대로 올린다(호출부에서 판단).
    """
    for doc in _documents(_COORD2REGION_URL, {"x": longitude, "y": latitude}, timeout):
        name = (doc.get("region_1depth_name") or "").strip()
        if name:
            return _SHORT_REGION.get(name, name)
    return None


# ── 최근접 역 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NearStation:
    """관광지에서 가장 가까운 역. distance_m은 카카오가 주는 **직선거리**다."""

    name: str            # "양천향교역" — 노선 접미사를 뗀 역명
    line: str | None     # "9호선"·"경춘선" 등. 기차역이면 None
    distance_m: int


def _split_line(place_name: str) -> tuple[str, str | None]:
    """카카오 place_name을 역명과 노선으로 가른다.

    지하철역은 "양천향교역 9호선"처럼 노선이 뒤에 붙고 기차역은 "여수엑스포역"처럼 붙지 않는다.
    역명 자체에는 공백이 없어("국제금융센터.부산은행역") 첫 공백에서 자르면 된다.
    """
    name, _, line = place_name.partition(" ")
    return name, (line or None)


def _boardable(doc: dict) -> bool:
    """승객이 실제로 탈 수 있는 역만 남긴다.

    키워드 검색("기차역")은 상호까지 걸려들고("이디야커피 신촌기차역점", "신촌기차역입구교차로"),
    역 중에도 못 타는 것들이 섞인다 — 폐역, 화물 전용역("부산진화물역"), 신호장("모량신호장").
    """
    category = doc.get("category_name") or ""
    name = doc.get("place_name") or ""
    if "폐역" in category or "화물" in name or "신호장" in name:
        return False
    return "지하철,전철" in category or "기차,철도 > 기차역" in category


def nearest_station(
    latitude: float, longitude: float, radius_m: int, timeout: int = 5,
) -> NearStation | None:
    """좌표에서 radius_m 안에 있는 가장 가까운 역. 없으면 None.

    **지하철역과 기차역을 함께 본다.** 코레일 기차역만 보면 도시 관광지가 엉뚱한 답을 받는다 —
    서울 강서구엔 기차역이 없어 한강 건너 행신역(4.7km)이 '가장 가까운 역'이 되는 식이다.
    실제로 걸어가는 역은 양천향교역(628m)이고, 그건 지하철역이라야 잡힌다.

    카카오는 radius 밖이면 빈 응답을 주므로 **역이 없는 지역(제주·산간)은 자연스럽게 None**이다.
    거리는 직선거리라 도보 시간은 호출부에서 우회 보정으로 근사한다.
    HTTP/파싱 실패는 예외를 그대로 올린다(호출부에서 판단).
    """
    common = {"x": longitude, "y": latitude, "radius": radius_m,
              "sort": "distance", "size": _SEARCH_SIZE}
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_train = ex.submit(
            _documents, _URL, {"query": _TRAIN_QUERY, **common}, timeout
        )
        subway = _documents(
            _CATEGORY_URL, {"category_group_code": _SUBWAY_GROUP, **common}, timeout
        )
        docs = subway + f_train.result()

    stations = [
        NearStation(*_split_line(d["place_name"]), int(d["distance"]))
        for d in docs
        if _boardable(d) and d.get("place_name") and d.get("distance")
    ]
    # 기차역을 앞에 두고 같은 역명은 첫 항목만 남긴다 — 서울역·부전역처럼 기차역과 지하철역이
    # 겹치는 곳에서 "서울역 1호선"이 아니라 "서울역"으로 나와야 기차 여행 앱의 문구가 된다.
    stations.sort(key=lambda s: (s.line is not None, s.distance_m))
    found: dict[str, NearStation] = {}
    for station in stations:
        found.setdefault(station.name, station)

    if not found:
        return None
    return min(found.values(), key=lambda s: s.distance_m)
