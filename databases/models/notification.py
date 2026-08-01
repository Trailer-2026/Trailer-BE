from sqlalchemy import Boolean, Column, ForeignKey, Integer, text

from databases.models.base import BaseModel


class Notification(BaseModel):
    __tablename__ = 'notification'
    __table_args__ = {'comment': '알림 수신 설정 (사용자당 1행)'}

    notification_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    user_idx = Column(
        Integer, ForeignKey('user.user_idx'), nullable=False, unique=True,
        comment="사용자 FK (1:1 — 사용자당 설정 1행)",
    )
    # 기본값은 둘 다 수신(true) — 설정 행이 없는 기존 사용자도 켜진 것으로 보이게 맞춘다.
    event_alarm = Column(
        Boolean, nullable=False, default=True, server_default=text("true"),
        comment="이벤트 알림 수신 여부",
    )
    scenery_alarm = Column(
        Boolean, nullable=False, default=True, server_default=text("true"),
        comment="기차역 풍경 알림 수신 여부",
    )
