"""내 영상 업로드 자체 점검 — `python tests/test_reels_upload.py`.

GCS·ffprobe·ffmpeg 은 스텁으로 갈음한다(네트워크·바이너리 없이 도는 게 목적).
프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.
"""
import io
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.exceptions.custom import BadRequestException
from databases.models.base import Base
from databases.models.reels import Reels
from databases.models.user import User
from services import video_service
from utils import gcs


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (User, Reels)])
    db = sessionmaker(bind=engine)()
    db.add(User(user_idx=1, nickname="tester", provider="google", provider_id="t"))
    db.commit()
    return db


def _stub_all(monkey: dict):
    """업로드 경로가 건드리는 외부 의존을 전부 스텁으로 바꾼다."""
    uploaded = []
    monkey["upload_file"] = gcs.upload_file
    monkey["ffprobe"] = video_service._ffprobe_video
    monkey["thumb"] = video_service._publish_thumbnail
    monkey["which"] = shutil.which

    gcs.upload_file = lambda path, src, ctype: (
        uploaded.append((path, ctype)) or f"https://storage.googleapis.com/b/{path}"
    )
    video_service._ffprobe_video = lambda path: {
        "duration": 12.0, "has_audio": True, "width": 1080,
        "height": 1920, "fps": "30/1", "sample_rate": 44100,
    }
    video_service._publish_thumbnail = lambda path, at=3.5: f"thumb://{at}"
    shutil.which = lambda name: f"/usr/bin/{name}"
    return uploaded


def _restore(monkey: dict):
    gcs.upload_file = monkey["upload_file"]
    video_service._ffprobe_video = monkey["ffprobe"]
    video_service._publish_thumbnail = monkey["thumb"]
    shutil.which = monkey["which"]


def _expect_bad_request(fn, what: str):
    try:
        fn()
    except BadRequestException:
        return
    raise AssertionError(f"{what}: BadRequestException 이 나야 한다")


def main() -> None:
    monkey: dict = {}
    uploaded = _stub_all(monkey)
    db = _session()
    try:
        # 정상 업로드 → 릴스 행이 url·썸네일·제목까지 채워져 생긴다.
        result = video_service.upload_reels(
            db, 1, "trip.MOV", io.BytesIO(b"fake-video-bytes"), "  부산 2박 3일  "
        )
        reels = db.query(Reels).one()
        assert result.reels_idx == reels.reels_idx, result
        assert reels.url == result.url and result.url.endswith(".mov"), result.url
        assert reels.thumbnail_url == result.thumbnail_url, result
        assert reels.title == "부산 2박 3일", reels.title  # 공백 정규화
        assert reels.region is None, reels.region
        assert result.duration_seconds == 12.0, result
        # 확장자를 살려야 mov 가 브라우저에서 재생된다.
        assert uploaded[-1][1] == "video/quicktime", uploaded

        # 제목이 공백뿐이면 제목 없는 릴스.
        result = video_service.upload_reels(db, 1, "a.mp4", io.BytesIO(b"x"), "   ")
        assert result.title is None, result

        # 영상이 아닌 파일 / 확장자 없음 → 400 (업로드 시도조차 안 한다)
        before = len(uploaded)
        _expect_bad_request(
            lambda: video_service.upload_reels(db, 1, "doc.pdf", io.BytesIO(b"x"), ""),
            "영상 아님",
        )
        _expect_bad_request(
            lambda: video_service.upload_reels(db, 1, "noext", io.BytesIO(b"x"), ""),
            "확장자 없음",
        )
        assert len(uploaded) == before, "영상이 아니면 버킷에 올리면 안 된다"

        # 빈 파일 → 400
        _expect_bad_request(
            lambda: video_service.upload_reels(db, 1, "a.mp4", io.BytesIO(b""), ""),
            "빈 파일",
        )

        # 상한 초과 → 400 (버킷 업로드 전에 끊긴다)
        before = len(uploaded)
        big = io.BytesIO(b"\0" * (video_service.MAX_UPLOAD_BYTES + 1))
        _expect_bad_request(
            lambda: video_service.upload_reels(db, 1, "big.mp4", big, ""),
            "크기 초과",
        )
        assert len(uploaded) == before, "상한 초과분이 버킷에 올라갔다"

        # 짧은 영상은 중간 지점에서 썸네일을 뽑는다 (3.5초 고정이면 프레임이 안 나온다).
        video_service._ffprobe_video = lambda path: {
            "duration": 2.0, "has_audio": False, "width": 720,
            "height": 1280, "fps": "30/1", "sample_rate": 44100,
        }
        result = video_service.upload_reels(db, 1, "short.mp4", io.BytesIO(b"x"), "")
        assert result.thumbnail_url == "thumb://1.0", result.thumbnail_url

        # 릴스 행은 정상 업로드 3건만 남아야 한다(실패 경로는 행을 안 만든다).
        assert db.query(Reels).count() == 3, db.query(Reels).count()
        print("OK: 내 영상 업로드 자체 점검 통과")
    finally:
        db.close()
        _restore(monkey)


if __name__ == "__main__":
    main()
