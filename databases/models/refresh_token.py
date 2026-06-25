from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Index
from databases.models.base import BaseModel


class RefreshToken(BaseModel):
    __tablename__ = 'refresh_token'
    __table_args__ = (
        Index('ix_refresh_token_user_idx', 'user_idx'),
        {'comment': 'refresh 토큰 화이트리스트'},
    )

    token_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    user_idx = Column(
        Integer, ForeignKey('user.user_idx'), nullable=False, comment="사용자 FK"
    )
    jti = Column(String(36), nullable=False, unique=True, comment="refresh 토큰 고유 ID (JWT jti)")
    expires_at = Column(DateTime(timezone=True), nullable=False, comment="만료 시각")
    revoked = Column(Boolean, nullable=False, default=False, comment="무효화 여부")
