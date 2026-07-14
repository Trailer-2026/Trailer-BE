from sqlalchemy import Column, Integer, String, Text, ForeignKey

from databases.models.base import BaseModel


class Comment(BaseModel):
    """릴스 댓글. parent_idx가 있으면 그 댓글에 달린 답글."""

    __tablename__ = "comment"
    __table_args__ = ({"comment": "댓글"},)

    comment_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    reels_idx = Column(
        Integer, ForeignKey("reels.reels_idx"), nullable=False, index=True, comment="FK 릴스"
    )
    user_idx = Column(
        Integer, ForeignKey("user.user_idx"), nullable=False, index=True, comment="FK 작성자"
    )
    parent_idx = Column(
        Integer,
        ForeignKey("comment.comment_idx"),
        nullable=True,
        index=True,
        comment="FK 부모 댓글 (답글이면 세팅, 최상위면 NULL)",
    )
    content = Column(Text, nullable=False, comment="내용")
    # ponytail: travel_idx는 reels 조인으로 나오므로 뺐다.
