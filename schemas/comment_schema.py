from datetime import datetime

from pydantic import BaseModel, Field


class CommentCreateRequest(BaseModel):
    """댓글 작성 요청. parent_idx를 주면 그 댓글의 답글로 달린다."""

    content: str = Field(..., min_length=1, max_length=1000, description="댓글 내용")
    parent_idx: int | None = Field(
        None, description="답글이면 부모 댓글 PK. 최상위 댓글이면 생략(null)"
    )


class CommentUpdateRequest(BaseModel):
    """댓글 수정 요청 — 내용만 바꾼다."""

    content: str = Field(..., min_length=1, max_length=1000, description="수정할 댓글 내용")


class CommentResponse(BaseModel):
    """댓글 1건. replies에 답글이 담긴다(최상위 댓글에만, 답글의 replies는 항상 빈 배열)."""

    comment_idx: int = Field(..., description="댓글 PK")
    reels_idx: int = Field(..., description="릴스 PK")
    user_idx: int = Field(..., description="작성자 PK")
    nickname: str | None = Field(None, description="작성자 닉네임")
    content: str = Field(..., description="댓글 내용")
    parent_idx: int | None = Field(None, description="부모 댓글 PK (답글이면 세팅)")
    created_at: datetime | None = Field(None, description="작성일")
    replies: list["CommentResponse"] = Field(default_factory=list, description="답글 목록")
