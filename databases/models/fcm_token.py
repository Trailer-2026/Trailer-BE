from sqlalchemy import Column, Integer, String, ForeignKey, Index
from databases.models.base import BaseModel


class FcmToken(BaseModel):
    __tablename__ = 'fcm_token'
    __table_args__ = (
        Index('ix_fcm_token_user_idx', 'user_idx'),
        {'comment': 'FCM 푸시 토큰 (기기별)'},
    )

    fcm_token_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    user_idx = Column(
        Integer, ForeignKey('user.user_idx'), nullable=False, comment="사용자 FK"
    )
    token = Column(String(255), nullable=False, unique=True, comment="FCM 기기 토큰")
