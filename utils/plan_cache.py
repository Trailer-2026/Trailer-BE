"""추천 플랜 임시 캐시 — 추천 응답의 각 플랜을 짧은 TTL로 서버가 들고 있다가,
저장 요청 때 plan_id로 그 플랜을 그대로 꺼내 저장하기 위한 인메모리 저장소.

- 목적: 프론트가 긴 코스 payload를 되보내지 않고 plan_id만 보내 저장(본 그대로 저장, 재계산 X).
- 단일 워커 전제의 인메모리 dict. 다중 워커/인스턴스로 확장 시 Redis 등 공유 저장소로 교체.
- TTL 만료·서버 재시작 시 캐시 미스 → 저장 서비스가 '재추천' 안내로 처리한다.
"""
import threading
import time
import uuid

_TTL_SECONDS = 30 * 60   # 추천 후 저장까지 여유(30분)
_MAXSIZE = 5000          # 메모리 상한(초과 시 만료 임박 순으로 축출)
_lock = threading.Lock()
_store: dict[str, tuple[float, dict]] = {}  # plan_id -> (만료시각, payload)


def put(payload: dict) -> str:
    """payload를 저장하고 plan_id를 발급한다."""
    plan_id = uuid.uuid4().hex
    with _lock:
        _evict_locked()
        _store[plan_id] = (time.time() + _TTL_SECONDS, payload)
    return plan_id


def get(plan_id: str) -> dict | None:
    """plan_id의 payload. 없거나 만료면 None."""
    with _lock:
        item = _store.get(plan_id)
        if item is None:
            return None
        expiry, payload = item
        if expiry < time.time():
            _store.pop(plan_id, None)
            return None
        return payload


def _evict_locked() -> None:
    """만료분 제거 + 상한 초과 시 만료 임박 순으로 축출 (호출자가 락 보유)."""
    now = time.time()
    for k in [k for k, (exp, _) in _store.items() if exp < now]:
        _store.pop(k, None)
    if len(_store) >= _MAXSIZE:
        oldest = sorted(_store.items(), key=lambda kv: kv[1][0])[: len(_store) - _MAXSIZE + 1]
        for k, _ in oldest:
            _store.pop(k, None)
