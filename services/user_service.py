import uuid

from sqlalchemy.orm import Session

from databases.daos import user_dao
from databases.models.user import User
from core.exceptions.custom import BadRequestException
from schemas.user_schema import ProfileResponse
from utils import gcs

MAX_PROFILE_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB


def get_profile(user: User) -> ProfileResponse:
    """내 정보 / 프로필 설정 통합 조회. 연동 소셜 계정은 유저당 1개(가입 provider)."""
    return ProfileResponse(
        user_idx=user.user_idx,
        nickname=user.nickname,
        email=user.email,
        profile_image=user.profile_image,
        provider=user.provider,
    )


def update_nickname(db: Session, user: User, nickname: str) -> ProfileResponse:
    user_dao.update(db, user, nickname=nickname.strip())
    db.commit()
    return get_profile(user)


def update_profile_image(
    db: Session, user: User, data: bytes, content_type: str | None, filename: str | None
) -> ProfileResponse:
    """업로드한 이미지를 GCS에 올리고 프로필 사진 URL을 갱신한다."""
    if not content_type or not content_type.startswith("image/"):
        raise BadRequestException("이미지 파일만 업로드할 수 있습니다.")
    if not data:
        raise BadRequestException("빈 파일입니다.")
    if len(data) > MAX_PROFILE_IMAGE_BYTES:
        raise BadRequestException("프로필 사진은 10MB 이하만 가능합니다.")

    ext = ""
    if filename and "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()
    url = gcs.upload_bytes(f"profile/{user.user_idx}/{uuid.uuid4().hex}{ext}", data, content_type)
    # ponytail: 이전 프로필 이미지는 GCS에 그대로 남긴다. 고아 객체 정리가 필요해지면 delete_object 추가.
    user_dao.update(db, user, profile_image=url)
    db.commit()
    return get_profile(user)
