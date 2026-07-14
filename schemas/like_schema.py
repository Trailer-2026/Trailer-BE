from pydantic import BaseModel, Field


class LikeResponse(BaseModel):
    """좋아요/취소 후의 상태 — 클라이언트가 하트 UI를 바로 갱신할 수 있게 개수까지 준다."""

    liked: bool = Field(..., description="이 사용자가 현재 좋아요한 상태인지")
    like_count: int = Field(..., description="대상의 총 좋아요 수")
