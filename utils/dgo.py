"""data.go.kr(공공데이터포털) 공용 헬퍼 — tour_api·train_api·train_stops가 공유.

세 클라이언트가 복붙하던 (1) GET 요청, (2) `items.item` 정규화만 모은다.
**에러 검사는 여기 넣지 않는다** — API마다 성공 resultCode 규약이 달라(TAGO '00' vs
TourAPI '0000') 공통 검사가 오히려 정상 응답을 오류로 만든다. 규약 해석은 각 클라이언트 몫.
"""
import json
import urllib.parse
import urllib.request


def get_body(base_url: str, params: dict, timeout: int) -> dict:
    """GET 1콜 → response.body(dict). data.go.kr는 정상/오류 모두 HTTP 200이라 파싱만 한다.

    오류 응답(response/body 없음)이면 KeyError가 그대로 올라가 호출측이 502로 변환한다.
    """
    url = base_url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)["response"]["body"]


def items(body: dict) -> list[dict]:
    """body.items.item 을 항상 list로 정규화한다(0건이면 "" 로 와서 [], 1건이면 [dict])."""
    it = body.get("items")
    if not it:  # 0건이면 "" (data.go.kr 특성)
        return []
    item = it.get("item")
    if item is None:
        return []
    return item if isinstance(item, list) else [item]
