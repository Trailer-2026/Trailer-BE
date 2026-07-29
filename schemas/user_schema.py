from typing import Optional

from pydantic import BaseModel, Field


class ProfileResponse(BaseModel):
    """내 정보 / 프로필 설정 화면에 쓰는 통합 프로필.

    연동 소셜 계정은 유저당 1개(가입한 provider)라 provider 단일 필드로 내려준다.
    """

    user_idx: int = Field(..., description="사용자 PK", examples=[2])
    nickname: Optional[str] = Field(None, description="닉네임", examples=["멋진 오이소박이"])
    email: Optional[str] = Field(None, description="이메일", examples=["abcd@gmail.com"])
    profile_image: Optional[str] = Field(
        None, description="프로필 사진 URL (없으면 null → 프론트 기본 이미지)", examples=[None]
    )
    provider: str = Field(
        ..., description="연동된 소셜 제공자 (google | kakao)", examples=["kakao"]
    )


class NicknameUpdateRequest(BaseModel):
    """닉네임 편집 요청."""

    nickname: str = Field(
        ..., min_length=1, max_length=20, description="새 닉네임 (1~20자)", examples=["멋진 오이소박이"]
    )
