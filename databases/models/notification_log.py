from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String

from databases.models.base import BaseModel


class NotificationLog(BaseModel):
    """발송된 알림 1건 — 앱의 '알림 화면' 목록에 쌓이는 이력.

    수신 on/off 설정인 notification(사용자당 1행)과는 다른 테이블이다.
    설정은 notification, 실제로 보낸 내역은 notification_log.

    여기 쌓이는 알림은 모두 여행에 딸린 것(추가·D-1·삭제)이라 대상을 travel_idx로
    바로 잡는다. 풍경 알림은 푸시로만 나가고 이력을 남기지 않는다(push_service 참조).
    """

    __tablename__ = 'notification_log'
    __table_args__ = (
        # 알림 화면의 커서 페이징 질의(user_idx로 좁히고 PK 역순) 그대로를 태운다.
        # 정렬 키를 created_at이 아닌 PK로 잡는 이유는 dao.list_by_user 참조.
        Index('ix_notification_log_user_idx', 'user_idx', 'notification_log_idx'),
        {'comment': '발송된 알림 이력 (알림 화면 목록)'},
    )

    notification_log_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    user_idx = Column(
        Integer, ForeignKey('user.user_idx'), nullable=False, comment="FK 사용자"
    )
    type = Column(
        String(20), nullable=False, comment="TRAVEL_SAVED | TRAVEL_D1 | TRAVEL_DELETED"
    )
    title = Column(String(100), nullable=False, comment="알림 제목")
    body = Column(String(255), nullable=False, comment="알림 본문")
    travel_idx = Column(
        Integer, ForeignKey('travel.travel_idx'), nullable=True,
        comment="FK 여행 — 알림을 탭했을 때 열 여행이자 중복 발송 판정 키(D-1은 여행당 1회). "
                "여행과 무관한 알림이 생길 수 있어 nullable",
    )
    read_at = Column(
        DateTime(timezone=True), nullable=True, comment="읽은 시각 (null이면 안 읽음)"
    )
