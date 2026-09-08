# -*- coding: utf-8 -*-
"""중단된 렌더 정리 자체 점검 — `python tests/test_render_sweep.py`.

프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.
DB 는 인메모리 SQLite, 파일은 임시 디렉터리로 갈음한다.

지키려는 규칙: 재시작으로 중단된 렌더의 **자리표 릴스 행은 지우되 폴링하던 쪽은
404 대신 사유를 받고**, 완성된 릴스는 건드리지 않으며, 잔여 파일은 **오래된 것만**
지운다(살아 있는 렌더의 입력을 지우면 안 된다). 실패 사유에 서버 로그를 싣지 않는다.
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENAPI_EXPORT", "1")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from databases.models.base import Base
from databases.models.reels import Reels
from databases.models.user import User
from services import video_service


def _session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (User, Reels)])
    factory = sessionmaker(bind=engine)
    db = factory()
    db.add(User(user_idx=1, nickname="tester", provider="google", provider_id="t"))
    db.add_all([
        # 렌더 중 재시작으로 남은 자리표 2건 + 정상 완료 1건.
        Reels(reels_idx=10, user_idx=1, url=video_service.PENDING_REELS_URL, title="중단1"),
        Reels(reels_idx=11, user_idx=1, url=video_service.PENDING_REELS_URL, title="중단2"),
        Reels(reels_idx=12, user_idx=1, url="https://storage/reels/done.mp4", title="완료"),
    ])
    db.commit()
    db.close()
    return factory


def _aged(path: Path, hours: float) -> Path:
    """파일/디렉터리의 mtime 을 hours 시간 전으로 돌린다."""
    old = time.time() - hours * 3600
    os.utime(path, (old, old))
    return path


def main() -> None:
    factory = _session_factory()
    # sweep 이 여는 세션(databases.database.SessionLocal)을 인메모리 DB 로 바꿔친다.
    import databases.database as database

    original_session_local = database.SessionLocal
    database.SessionLocal = factory

    uploads = Path(tempfile.mkdtemp(prefix="sweep_uploads_"))
    output = Path(tempfile.mkdtemp(prefix="sweep_output_"))
    original_dirs = (video_service.UPLOADS_DIR, video_service.OUTPUT_DIR)
    video_service.UPLOADS_DIR, video_service.OUTPUT_DIR = uploads, output

    # 오래된 잔여물 / 방금 만들어진 것(= 살아 있는 렌더의 입력일 수 있다)
    stale_dir = _aged(Path(tempfile.mkdtemp(dir=uploads)), video_service.STALE_UPLOAD_HOURS + 1)
    fresh_dir = Path(tempfile.mkdtemp(dir=uploads))
    stale_mp4 = output / "travel_3d_1.mp4"
    stale_mp4.write_bytes(b"x")
    _aged(stale_mp4, video_service.STALE_OUTPUT_DAYS * 24 + 1)
    fresh_mp4 = output / "travel_3d_2.mp4"
    fresh_mp4.write_bytes(b"x")

    try:
        video_service.sweep_stale_renders()

        # 1) 자리표 행만 사라지고 완성된 릴스는 그대로다.
        db = factory()
        try:
            left = sorted(r.reels_idx for r in db.query(Reels).all())
        finally:
            db.close()
        assert left == [12], f"자리표만 지워야 한다: {left}"

        # 2) 폴링하던 클라이언트는 404 가 아니라 사유를 받는다 (본인 것만).
        for reels_idx in (10, 11):
            status = video_service.get_render_job(None, reels_idx, user_idx=1)
            assert status["status"] == "failed", status
            assert status["phase"] == "중단됨", status
            assert status["error"] == video_service.INTERRUPTED_MESSAGE, status
            # 서버 로그가 사용자 응답에 섞이면 안 된다.
            assert "Traceback" not in status["error"] and "/" not in status["error"], status

        # 3) 잔여 파일은 오래된 것만 지운다 — 방금 만들어진 건 진행 중일 수 있다.
        assert not stale_dir.exists(), "오래된 업로드 디렉터리가 남았다"
        assert fresh_dir.exists(), "최근 업로드 디렉터리를 지웠다 — 진행 중 렌더의 입력일 수 있다"
        assert not stale_mp4.exists(), "오래된 mp4 가 남았다"
        assert fresh_mp4.exists(), "최근 mp4 를 지웠다 — 업로드 실패분 회수 시간을 안 줬다"

        # 4) 스윕은 부팅 경로라 어떤 실패도 삼킨다(부팅을 막으면 안 된다).
        database.SessionLocal = None  # 호출하면 TypeError
        video_service.sweep_stale_renders()
    finally:
        database.SessionLocal = original_session_local
        video_service.UPLOADS_DIR, video_service.OUTPUT_DIR = original_dirs
        shutil.rmtree(uploads, ignore_errors=True)
        shutil.rmtree(output, ignore_errors=True)
    print("OK: 중단된 렌더 정리 자체 점검 통과")


if __name__ == "__main__":
    main()
