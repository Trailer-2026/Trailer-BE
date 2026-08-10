from sqlalchemy.orm import Session

from databases.daos import user_dao
from databases.models.user import User
from schemas.user_schema import ProfileResponse
from utils import gcs


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
    """업로드한 이미지를 GCS에 올리고 프로필 사진 URL을 갱신한다(검증은 gcs.upload_image)."""
    url = gcs.upload_image(f"profile/{user.user_idx}", data, content_type, filename)
    # ponytail: 이전 프로필 이미지는 GCS에 그대로 남긴다. 고아 객체 정리가 필요해지면 delete_object 추가.
    user_dao.update(db, user, profile_image=url)
    db.commit()
    return get_profile(user)
