"""data.go.kr(공공데이터포털) 공용 헬퍼 — tour_api·train_api·train_stops가 공유.

세 클라이언트가 복붙하던 (1) GET 요청, (2) `items.item` 정규화, (3) 서비스 키 주입·로테이션만 모은다.

**성공 판정은 여기 넣지 않는다** — API마다 성공 resultCode 규약이 달라(TAGO '00' vs
TourAPI '0000') 공통 검사가 오히려 정상 응답을 오류로 만든다. 규약 해석은 각 클라이언트 몫.

반대로 **키 거부는 포털 공통 규약**이라 여기서 잡는다. 키가 거부되면(한도 소진·미등록·
서비스 장애) 그 키만 죽은 것으로 찍고 **다음 키로 즉시 재시도**하며, 살아 있는 키가 하나도
없을 때만 실패시킨다. 그리고 죽은 키로는 `TRIP_SECONDS` 동안 아예 네트워크를 타지 않는다 —
안 막으면 추천 검색 1회가 수백 건(서울→부산 4박5일 기준 250~900콜)의 실패 호출을 끝까지
다 쏘느라 요청이 타임아웃으로만 끝나고, 정작 원인은 어디에도 안 보인다.

설정: `[tourapi] service_key` 에 **콤마로 여러 키**를 둘 수 있다(키가 64자 영숫자라 충돌 없음).
    service_key = 키1,키2,키3
"""
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from config import Config
from core.exceptions.custom import ExternalServiceException

_KEY_SECTION, _KEY_NAME = "tourapi", "service_key"


class PublicApiRejectedError(ExternalServiceException):
    """설정된 서비스 키가 전부 거부됨 — 한도 소진·미등록·활용 중지·공공API 장애."""

    def __init__(self, message: str = "공공데이터포털 API 호출이 거부되었습니다. "
                                      "일일 호출 한도 초과, 서비스 키 미등록·활용기간 만료, "
                                      "또는 해당 공공API 장애일 수 있습니다."):
        super().__init__(message)


class KeyRejected(Exception):
    """내부 신호 — 이 키가 거부됐으니 다음 키로 넘어가라(`with_key`가 소비)."""


# 포털이 키 거부를 알릴 때 응답에 박히는 표식. 게이트웨이(cmmMsgHeader)와 서비스(header)
# 두 응답 형식을 다 커버하려고 원문 문자열에서 찾는다.
#   30 미등록 키 / 22 일일 트래픽 초과 / 20 활용 중지
#   TAGO는 게이트웨이는 통과시키고 서비스가 HTTP 200으로 이렇게 답한다:
#   {"header":{"resultCode":"01","resultMsg":"serviceKey: 서비스키는 필수입니다."}}
_KEY_ERROR_MARKS = (
    "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
    "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR",
    "SERVICE_ACCESS_DENIED_ERROR",
    "서비스키",
)
# 거부된 키를 다시 안 쓰는 시간(초). 일일 한도는 자정에 풀리므로 주기적으로 한 번씩만
# 재탐색하면 알아서 복구된다.
TRIP_SECONDS = 300
# HTTP 429 재시도 횟수와 대기(초) 범위. **429는 키가 죽은 게 아니다** — 초당 트래픽 제한이라
# 실측상 몇 초면 풀린다. 이걸 키 만료로 보고 TRIP_SECONDS 동안 차단하면 추천 검색 두세 번에
# 키 3개가 전부 죽어, 그 5분간 모든 사용자의 여정에서 기차가 통째로 빠진다(route_type "현지").
# 지터를 주는 이유: 16스레드가 동시에 429를 맞으므로 같은 시각에 깨면 그대로 다시 몰린다.
RATE_LIMIT_RETRIES = 2
RATE_LIMIT_BACKOFF = (0.4, 1.2)
# 최소 호출 간격(초) = 10콜/초. **재시도만으로는 못 막아서 필요하다** — 백오프로 미뤄봐야
# 16스레드가 다시 몰려 또 429다. 실측: TAGO는 약 17콜/초에서 첫 429라 여유를 뒀다.
# 추천 1회가 열차 조회 110~200콜이지만 게이트를 걸어도 지연은 그대로였다(병목이 여기가 아니다).
# TourAPI에는 이걸 더 조여봐야 소용없다 — 거긴 429가 속도가 아니라 오퍼레이션별 일일 쿼터
# 소진이라(X-RateLimit-Limit=1000, Remaining=0) 5콜/초로 낮춰도 429 건수가 그대로였다.
# 주의: 프로세스 안에서만 세는 게이트다. 워커를 N개로 띄우면 실제 속도가 N배가 되니
# 그 땐 이 값을 N으로 나누거나 외부 레이트리미터로 올려라.
MIN_CALL_INTERVAL = 0.1
_gate = threading.Lock()  # _locks 딕셔너리 자체만 보호한다(대기는 아래 scope별 락에서)
_locks: dict[str, threading.Lock] = {}
_last_call: dict[str, float] = {}


def _throttle(scope: str) -> None:
    """그 API로 나가는 호출을 MIN_CALL_INTERVAL 간격으로 직렬화한다.

    **락을 쥔 채로 잔다** — 그래야 대기 중인 스레드들이 동시에 깨어 한꺼번에 나가지 않는다.
    **락은 scope별로 따로 둔다** — 하나로 묶으면 열차 조회 대기가 관광지 조회까지 막아
    두 API의 대기가 더해진다(한 검색에 TAGO 190콜 + TourAPI 80콜이라 체감이 크다).
    """
    with _gate:
        lock = _locks.setdefault(scope, threading.Lock())
    with lock:
        now = time.monotonic()
        wait = _last_call.get(scope, 0.0) + MIN_CALL_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
            now += wait
        _last_call[scope] = now


# (scope, 키) → 거부된 시각(monotonic). **반드시 따로 센다** — TAGO가 키를 거부했다고 그 키로
# TourAPI까지 막으면 멀쩡한 관광지 조회가 죽는다. 얼마나 잘게 나눌지는 호출측이 정하는데,
# **쿼터가 걸리는 단위와 같아야 한다**: TourAPI는 일일 한도가 오퍼레이션별이라 tour_api가
# scope에 operation까지 넣는다(안 그러면 detailIntro2 소진이 관광지 목록까지 막는다).
_dead: dict[tuple[str, str], float] = {}


def live_keys(scope: str) -> list[str]:
    """그 API(scope)에서 아직 거부되지 않은 서비스 키 목록(설정 순서 유지)."""
    now = time.monotonic()
    raw = Config.read(_KEY_SECTION, _KEY_NAME) or ""
    return [
        k for k in (x.strip() for x in raw.split(",")) if k
        and now - _dead.get((scope, k), float("-inf")) >= TRIP_SECONDS
    ]


def raise_if_key_problem(text: str) -> None:
    """응답 원문이 키 거부면 KeyRejected. 아니면 그냥 통과(다른 오류는 호출측 몫)."""
    if any(m in text for m in _KEY_ERROR_MARKS):
        raise KeyRejected()


def with_key(call, scope: str):
    """살아 있는 키를 차례로 넣어 `call(key)`을 시도한다. 전부 거부되면 PublicApiRejectedError.

    `scope`는 **쿼터가 걸리는 단위**를 식별하는 문자열 — 죽은 키도 속도 제한도 이 단위다.
    보통 base URL이지만, 한도가 오퍼레이션별인 API는 거기까지 포함시켜라(tour_api 참고).
    `call`은 키 거부를 만나면 `raise_if_key_problem`(또는 HTTP 429 시 `KeyRejected`)으로
    신호를 올려야 다음 키로 넘어간다. 그 밖의 예외는 로테이션 없이 그대로 올라간다.

    **속도 제한(`_throttle`)도 여기서 건다.** 세 클라이언트(train_api·train_stops·tour_api)가
    전부 이 함수를 지나므로 여기 한 곳이면 우회가 없다. get_body 쪽에만 걸면 fetch_json을
    직접 부르는 tour_api가 게이트를 통째로 빠져나간다.
    """
    keys = live_keys(scope)
    if not keys:
        raise PublicApiRejectedError()
    for key in keys:
        try:
            _throttle(scope)
            return call(key)
        except KeyRejected:
            _dead[(scope, key)] = time.monotonic()
    raise PublicApiRejectedError()


def fetch_json(url: str, timeout: int) -> dict:
    """GET 1콜 → 응답 JSON(dict). 키 거부면 `KeyRejected`(→ `with_key`가 다음 키로 넘긴다).

    **파싱 전에 원문 문자열로 표식을 찾는다.** 게이트웨이 단계에서 막히면(한도 소진·미등록 키)
    `_type=json`을 줘도 XML(`cmmMsgHeader`)로 답하기 때문에, json으로 먼저 파싱하면
    JSONDecodeError가 터져 키 거부를 영영 못 알아보고 남은 수백 콜을 그대로 다 쏜다.

    **429는 잠깐 자고 같은 키로 재시도한다.** 초당 제한이라 몇 초면 풀리는데, 키를 갈아타봐야
    같은 제한에 그대로 걸린다(로테이션이 안 먹는 유일한 거부 사유). 재시도까지 다 429면
    그때는 `KeyRejected`로 넘겨 기존 경로(다음 키 → 전부 죽으면 502)를 그대로 탄다.
    """
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise_if_key_problem(e.read().decode("utf-8", "replace"))
                raise
            if attempt == RATE_LIMIT_RETRIES:
                raise KeyRejected() from e
            time.sleep(random.uniform(*RATE_LIMIT_BACKOFF) * (attempt + 1))
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise_if_key_problem(raw)  # 게이트웨이 오류는 JSON이 아니라 XML로 온다
        raise


def get_body(base_url: str, params: dict, timeout: int) -> dict:
    """GET 1콜 → response.body(dict). **serviceKey는 여기서 주입한다**(호출측은 넣지 마라).

    data.go.kr는 정상/오류 모두 HTTP 200이라 파싱만 한다. 키 거부는 다음 키로 재시도하고,
    그 밖의 오류 응답(response/body 없음)이면 KeyError가 그대로 올라가 호출측이 502로 변환한다.
    """
    def _call(key: str) -> dict:
        url = base_url + "?" + urllib.parse.urlencode({"serviceKey": key, **params})
        payload = fetch_json(url, timeout)
        if "response" not in payload:
            raise_if_key_problem(json.dumps(payload, ensure_ascii=False))
        return payload["response"]["body"]

    return with_key(_call, base_url)


def items(body: dict) -> list[dict]:
    """body.items.item 을 항상 list로 정규화한다(0건이면 "" 로 와서 [], 1건이면 [dict])."""
    it = body.get("items")
    if not it:  # 0건이면 "" (data.go.kr 특성)
        return []
    item = it.get("item")
    if item is None:
        return []
    return item if isinstance(item, list) else [item]


def _selfcheck() -> None:
    """키 로테이션·차단 셀프체크(네트워크 없음) — 실행: python -m utils.dgo."""
    import io

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    OK = b'{"response":{"body":{"items":""}}}'
    BAD = '{"header":{"resultCode":"01","resultMsg":"serviceKey: 서비스키는 필수입니다."}}'.encode()
    # 게이트웨이에서 막히면 _type=json이어도 XML로 답한다(실제 포털 응답 형태).
    BAD_XML = (b'<OpenAPI_ServiceResponse><cmmMsgHeader><returnAuthMsg>'
               b'LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR</returnAuthMsg>'
               b'<returnReasonCode>22</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>')
    A, B = "http://a", "http://b"  # 서로 다른 API 두 곳
    used = []
    dead_api = None  # 설정하면 그 API만 모든 키를 거부한다
    reject_body = BAD  # 거부 응답 형태(서비스 JSON / 게이트웨이 XML)
    throttle = 0  # 앞에서부터 이 횟수만큼 429를 돌려준다(초당 제한 흉내)

    def fake(url, timeout=None):
        nonlocal throttle
        key = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["serviceKey"][0]
        used.append(key)
        if throttle:
            throttle -= 1
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        rejects = key in rejected or (dead_api is not None and url.startswith(dead_api))
        return _Resp(reject_body if rejects else OK)

    orig_urlopen, orig_read, orig_sleep = urllib.request.urlopen, Config.read, time.sleep
    urllib.request.urlopen = fake
    time.sleep = lambda s: None  # 백오프 대기는 건너뛴다(셀프체크는 즉시 끝나야 한다)
    Config.read = staticmethod(lambda s, p, d=None: "k1,k2,k3" if (s, p) == (_KEY_SECTION, _KEY_NAME) else d)
    try:
        # 1) 첫 키가 살아 있으면 그것만 쓴다.
        _dead.clear(); used.clear(); rejected = set()
        assert get_body(A, {"a": 1}, 5) == {"items": ""}
        assert used == ["k1"], used

        # 2) k1이 거부되면 k2로 갈아타고, 이후엔 죽은 k1을 다시 안 쓴다.
        _dead.clear(); used.clear(); rejected = {"k1"}
        assert get_body(A, {}, 5) == {"items": ""}
        assert used == ["k1", "k2"], used
        used.clear()
        assert get_body(A, {}, 5) == {"items": ""}
        assert used == ["k2"], used  # k1은 건너뛴다

        # 3) 전부 거부되면 502 — 그리고 이후 호출은 네트워크를 아예 안 탄다(타임아웃 대신 즉시 실패).
        _dead.clear(); used.clear(); rejected = {"k1", "k2", "k3"}
        try:
            get_body(A, {}, 5)
            raise AssertionError("PublicApiRejectedError가 나야 한다")
        except PublicApiRejectedError:
            pass
        assert used == ["k1", "k2", "k3"], used
        used.clear()
        try:
            get_body(A, {}, 5)
            raise AssertionError("살아 있는 키가 없으면 즉시 실패해야 한다")
        except PublicApiRejectedError:
            pass
        assert used == [], "죽은 키로 네트워크를 탔다"

        # 4) 죽은 키는 그 API에서만 죽는다 — A가 키를 다 거부해도 B는 멀쩡히 첫 키로 나간다.
        #    (안 그러면 TAGO 장애 하나가 TourAPI 관광지 조회까지 5분간 막는다.)
        _dead.clear(); used.clear(); rejected = set(); dead_api = A
        try:
            get_body(A, {}, 5)
            raise AssertionError("A는 실패해야 한다")
        except PublicApiRejectedError:
            pass
        used.clear()
        assert get_body(B, {}, 5) == {"items": ""}
        assert used == ["k1"], used

        # 5) 게이트웨이 거부(XML)도 똑같이 잡아 다음 키로 넘어간다. 파싱부터 하면 여기서
        #    JSONDecodeError로 새어 키 거부를 못 알아보고 남은 수백 콜을 그대로 다 쏜다.
        _dead.clear(); used.clear(); rejected = {"k1"}; dead_api = None; reject_body = BAD_XML
        assert get_body(A, {}, 5) == {"items": ""}
        assert used == ["k1", "k2"], used

        # 6) 429는 키를 죽이지 않는다 — 같은 키로 백오프 재시도해서 통과하고, _dead에 안 찍힌다.
        #    (429를 키 만료로 보면 초당 제한 한 번에 키 전부가 5분간 죽어 기차가 통째로 빠진다.)
        _dead.clear(); used.clear(); rejected = set(); reject_body = BAD; throttle = 1
        assert get_body(A, {}, 5) == {"items": ""}
        assert used == ["k1", "k1"], used  # 키 로테이션 없이 같은 키로 재시도
        assert _dead == {}, _dead

        # 7) 재시도까지 다 429면 그때는 키 거부로 넘겨 기존 경로를 탄다(다음 키 → 전부 죽으면 502).
        _dead.clear(); used.clear(); throttle = 99
        try:
            get_body(A, {}, 5)
            raise AssertionError("계속 429면 PublicApiRejectedError가 나야 한다")
        except PublicApiRejectedError:
            pass
        assert used == ["k1"] * 3 + ["k2"] * 3 + ["k3"] * 3, used

        assert items({"items": ""}) == []
    finally:
        urllib.request.urlopen, Config.read, time.sleep = orig_urlopen, orig_read, orig_sleep
        _dead.clear()

    # 8) 레이트 게이트: 스레드를 몰아쳐도 scope별로 MIN_CALL_INTERVAL 간격이 지켜진다.
    #    (여기만 진짜로 잔다 — 위 블록은 time.sleep을 no-op으로 바꿔놨었다.)
    #    개별 간격이 아니라 총 소요로 본다 — 윈도우 타이머 해상도(~16ms) 탓에 콜 하나하나의
    #    간격은 흔들리지만, 정작 지켜야 하는 건 구간 평균 속도라 그게 맞는 판정이기도 하다.
    _last_call.clear(); _locks.clear()
    threads = [threading.Thread(target=lambda: _throttle("http://gate")) for _ in range(6)]
    t0 = time.monotonic()
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.monotonic() - t0
    assert elapsed >= MIN_CALL_INTERVAL * 5 * 0.9, f"게이트가 안 먹었다: {elapsed:.3f}s"

    # 9) 다른 scope는 서로 안 막는다 — 6+6콜을 두 API로 나눠 쏘면 12콜치가 아니라 6콜치 시간.
    _last_call.clear(); _locks.clear()
    threads = [threading.Thread(target=lambda s=s: _throttle(s))
               for s in ("http://x", "http://y") for _ in range(6)]
    t0 = time.monotonic()
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.monotonic() - t0
    assert elapsed < MIN_CALL_INTERVAL * 9, f"scope끼리 서로 막고 있다: {elapsed:.3f}s"

    # 10) 게이트는 with_key에 걸려 있다 — fetch_json을 직접 부르는 tour_api 경로도 반드시 탄다.
    #     (get_body에만 걸면 TourAPI 트래픽 전량이 게이트를 빠져나간다.)
    _last_call.clear(); _locks.clear()
    orig_urlopen, orig_read = urllib.request.urlopen, Config.read
    urllib.request.urlopen = lambda *_a, **_k: _Resp(OK)
    Config.read = staticmethod(lambda s, p, d=None: "k1" if (s, p) == (_KEY_SECTION, _KEY_NAME) else d)
    try:
        t0 = time.monotonic()
        for _ in range(4):  # tour_api._get과 같은 모양: with_key + fetch_json 직접 호출
            with_key(lambda k: fetch_json(f"http://tour?serviceKey={k}", 5), "http://tour")
        elapsed = time.monotonic() - t0
        assert elapsed >= MIN_CALL_INTERVAL * 3 * 0.9, f"fetch_json 경로가 게이트를 우회했다: {elapsed:.3f}s"
    finally:
        urllib.request.urlopen, Config.read = orig_urlopen, orig_read
        _last_call.clear(); _locks.clear(); _dead.clear()

    print("dgo selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
