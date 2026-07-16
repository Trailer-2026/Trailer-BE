from datetime import datetime

from pydantic import BaseModel, Field


class CommentCreateRequest(BaseModel):
    """댓글 작성 요청. parent_idx를 주면 그 댓글의 답글로 달린다."""

    content: str = Field(
        ..., min_length=1, max_length=1000, description="댓글 내용", examples=["여기 진짜 좋네요"]
    )
    parent_idx: int | None = Field(
        None, description="답글이면 부모 댓글 PK. 최상위 댓글이면 생략(null)", examples=[None]
    )


class CommentUpdateRequest(BaseModel):
    """댓글 수정 요청 — 내용만 바꾼다."""

    content: str = Field(
        ..., min_length=1, max_length=1000, description="수정할 댓글 내용", examples=["오타 고쳤어요"]
    )


class CommentResponse(BaseModel):
    """댓글 1건. replies에 답글이 담긴다(최상위 댓글에만, 답글의 replies는 항상 빈 배열)."""

    comment_idx: int = Field(..., description="댓글 PK", examples=[12])
    reels_idx: int = Field(..., description="릴스 PK", examples=[5])
    user_idx: int = Field(..., description="작성자 PK", examples=[2])
    nickname: str | None = Field(None, description="작성자 닉네임", examples=["여행하는너구리"])
    content: str = Field(..., description="댓글 내용", examples=["여기 진짜 좋네요"])
    parent_idx: int | None = Field(
        None, description="부모 댓글 PK (답글이면 세팅)", examples=[None]
    )
    created_at: datetime | None = Field(
        None, description="작성일", examples=["2026-07-13T16:15:46+09:00"]
    )
    like_count: int = Field(0, description="이 댓글의 좋아요 수", examples=[2])
    liked: bool = Field(
        False, description="로그인한 사용자가 이 댓글에 좋아요를 눌렀는지", examples=[True]
    )
    replies: list["CommentResponse"] = Field(
        default_factory=list, description="답글 목록", examples=[[]]
    )
