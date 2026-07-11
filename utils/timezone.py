from datetime import datetime, timedelta, timezone

# 한국은 DST가 없어 고정 +09:00으로 둔다.
KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    """현재 시각(KST, tz-aware)."""
    return datetime.now(KST)
