from sqlalchemy import Column, Integer, String, ForeignKey

from databases.models.base import BaseModel


class Notification(BaseModel):
    """사용자 알림 1건 — 알림 화면 하단 리스트("...일정에 추가되었어요")용.

    데모 범위라 종류(type)와 표시 문구(message)만 저장한다. 표시 시각은 BaseModel의
    created_at을 그대로 쓴다("14분 전" 등 상대시간은 프론트가 계산).
    """

    __tablename__ = "notification"
    __table_args__ = ({"comment": "사용자 알림"},)

    notification_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    user_idx = Column(
        Integer, ForeignKey("user.user_idx"), nullable=False, index=True, comment="FK 사용자"
    )
    type = Column(String(30), nullable=False, default="TRAVEL_ADDED", comment="알림 종류 (예: TRAVEL_ADDED)")
    message = Column(String(255), nullable=False, comment="표시 문구")
