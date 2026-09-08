# -*- coding: utf-8 -*-
"""렌더 진행률 응답에 서버 로그가 안 실리는지 자체 점검 — `python tests/test_render_status_no_logs.py`.

프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.

지키려는 규칙: GET /api/videos/render/{reels_idx} 응답에는 **사용자에게 그대로 보여줄
문구만** 담긴다. 렌더 서브프로세스의 stdout·예외 메시지·서버 파일 경로는 서버 로그에만
남는다. 한때 log_tail 필드로 stdout 2KB 가 그대로 나갔던 자리다(성공 응답에도 실렸다).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ.setdefault("OPENAPI_EXPORT", "1")

from schemas.video_schema import VideoRenderStatusResponse
from services import video_service

# 실제 렌더 로그에 섞여 나오던 것들 — 응답 어디에도 이 조각이 있으면 안 된다.
SECRETS = (
    "Traceback (most recent call last):",
    "/home/dabin/Trailer-BE/services/videoMaker/render_video.py",
    "MAPBOX_ACCESS_TOKEN=pk.eyJ1Ijoi",
    "modal.exception.RemoteError",
    "output/travel_3d_7.mp4",
)


def _serialized(job: dict) -> str:
    """job 을 실제 응답 경로(_job_snapshot → 응답 모델)로 통과시켜 직렬화한다."""
    snapshot = video_service._job_snapshot(job)
    return VideoRenderStatusResponse.model_validate(snapshot).model_dump_json()


def main() -> None:
    # 1) 응답 모델에 로그를 담는 필드 자체가 없어야 한다(있으면 무엇을 넣든 새어 나간다).
    fields = set(VideoRenderStatusResponse.model_fields)
    assert "log_tail" not in fields, "log_tail 필드가 되살아났다 — 원시 로그가 응답으로 나간다"
    assert not [f for f in fields if "log" in f.lower()], f"로그성 필드가 생겼다: {fields}"

    # 2) job 에 로그가 섞여 있어도 응답에는 안 나온다(내부 필드는 _job_snapshot 이 뗀다).
    dirty = "\n".join(SECRETS)
    for status, extra in (
        ("failed", {"error": video_service.FAILED_MESSAGE}),
        ("done", {"video_url": "https://storage/reels/x.mp4"}),
    ):
        job = video_service._new_job(1, 1, status=status, job_dir=f"/srv/uploads/{dirty}", **extra)
        job["log_tail"] = dirty  # 옛 코드가 넣던 자리 — 남아 있어도 새면 안 된다
        payload = _serialized(job)
        for secret in SECRETS:
            assert secret not in payload, f"{status} 응답에 서버 로그가 실렸다: {secret}"

    # 3) 사용자 문구는 그대로 나가야 한다(마스킹한다고 사유까지 사라지면 안 된다).
    job = video_service._new_job(1, 1, status="failed", error=video_service.FAILED_MESSAGE)
    assert video_service.FAILED_MESSAGE in _serialized(job), "실패 사유가 응답에서 사라졌다"

    # 4) 사용자 문구 자체에 서버 경로·예외 이름이 섞여 있으면 안 된다.
    for message in (video_service.FAILED_MESSAGE, video_service.INTERRUPTED_MESSAGE):
        assert "/" not in message and "Traceback" not in message, message

    print("OK: 렌더 진행률 응답 서버 로그 비노출 자체 점검 통과")


if __name__ == "__main__":
    main()
