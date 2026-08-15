from pydantic import BaseModel, Field


class BlockedUser(BaseModel):
    """차단한 사용자 1명."""

    user_idx: int = Field(..., description="차단 당한 사용자 PK")
    nickname: str | None = Field(None, description="차단 당한 사용자 닉네임")
    profile_image: str | None = Field(
        None, description="차단 당한 사용자 프로필 사진 URL (없으면 null → 프론트 기본 이미지)",
        examples=[None],
    )
