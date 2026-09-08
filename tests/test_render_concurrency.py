# -*- coding: utf-8 -*-
"""렌더 동시 실행 상한 자체 점검 — `python tests/test_render_concurrency.py`.

프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.
실제 렌더(_render_job)는 스텁으로 갈음한다 — 여기서 보려는 건 슬롯 관리뿐이다.

지키려는 규칙: **동시에 도는 렌더가 RENDER_CONCURRENCY 를 넘지 않고**, 넘친 요청은
거절이 아니라 대기하며(끝내 전부 실행된다), 대기 시간은 ETA 계산에 안 들어가고,
환경변수가 쓰레기여도 서버가 뜬다(기본값 3).
"""
import importlib
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# config/properties_dev.ini 가 없는 환경(CI)에서도 임포트가 되게.
os.environ.setdefault("OPENAPI_EXPORT", "1")

from services import video_service


def _reload(value: str | None) -> None:
    """RENDER_CONCURRENCY 를 value 로 두고 모듈을 다시 읽는다 (None 이면 미설정)."""
    if value is None:
        os.environ.pop("RENDER_CONCURRENCY", None)
    else:
        os.environ["RENDER_CONCURRENCY"] = value
    importlib.reload(video_service)


def _check_env_parsing() -> None:
    """상한은 환경변수로 조절하되, 값이 이상해도 부팅을 막으면 안 된다."""
    _reload(None)
    assert video_service.RENDER_CONCURRENCY == 3, video_service.RENDER_CONCURRENCY
    _reload("5")
    assert video_service.RENDER_CONCURRENCY == 5, video_service.RENDER_CONCURRENCY
    # 배포 환경변수는 사람이 손으로 넣는다 — 오타·빈 값·0 에 서버가 죽으면 안 된다.
    # "²" 는 isdigit() 이 True 라 숫자로 보이지만 int() 는 ValueError 를 낸다.
    for bad in ("", "쓰레기", "-1", "0", "2.5", "²", " "):
        _reload(bad)
        assert video_service.RENDER_CONCURRENCY == 3, f"{bad!r} → {video_service.RENDER_CONCURRENCY}"
    _reload(None)


def _check_concurrency(limit: int = 2, jobs: int = 6) -> None:
    """슬롯 수만큼만 동시에 돌고, 넘친 요청도 결국 전부 실행된다."""
    video_service._render_slots = threading.BoundedSemaphore(limit)
    video_service._discard_pending_reels = lambda reels_idx: None  # DB 접근 차단

    lock = threading.Lock()
    running, peak, done = 0, 0, []

    def fake_render(job, command):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        # 슬롯을 쥔 채 겹칠 시간을 준다 — 상한이 없으면 여기서 전원이 겹친다.
        time.sleep(0.05)
        with lock:
            running -= 1
            done.append(job["reels_idx"])
        job["status"] = "done"

    video_service._render_job = fake_render

    created_at = time.time()
    # job_dir 없이 만든다 — 정리(rmtree)는 이 점검의 관심사가 아니다.
    made = [
        {"reels_idx": i, "status": "running", "phase": "렌더 준비 중", "started_at": created_at}
        for i in range(jobs)
    ]
    threads = [
        threading.Thread(target=video_service._run_render_job, args=(job, []))
        for job in made
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert peak <= limit, f"동시 실행 {peak} 편 — 상한 {limit} 을 넘었다"
    assert peak == limit, f"동시 실행이 {peak} 편뿐 — 슬롯을 놓지 않는다"
    assert sorted(done) == list(range(jobs)), f"대기하던 렌더가 유실됐다: {sorted(done)}"

    # 대기한 job 은 렌더 시작 시각이 다시 잡혀야 한다 — 안 그러면 ETA 가 대기 시간만큼
    # 부풀어 "5% 인데 1시간 남음"이 나간다.
    assert any(job["started_at"] > created_at for job in made), "started_at 을 다시 안 잡았다"


def main() -> None:
    _check_env_parsing()
    _check_concurrency()
    print("OK: 렌더 동시 실행 상한 자체 점검 통과")


if __name__ == "__main__":
    main()
