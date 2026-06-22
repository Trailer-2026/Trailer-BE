from sqlalchemy import Column, Integer, String, UniqueConstraint
from databases.models.base import BaseModel


class User(BaseModel):
    __tablename__ = 'user'
    __table_args__ = (
        UniqueConstraint('provider', 'provider_id', name='uq_user_provider'),
        {'comment': '사용자'},
    )

    user_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    provider = Column(String(20), nullable=False, comment="소셜 제공자 (google | kakao)")
    provider_id = Column(String(100), nullable=False, comment="소셜 고유 ID (google sub / kakao id)")
    email = Column(String(255), nullable=True, comment="이메일")
