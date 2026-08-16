from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, text

from databases.models.base import BaseModel


class NotificationLog(BaseModel):
    """발송된 알림 1건 — 앱의 '알림 화면' 목록에 쌓이는 이력.

    수신 on/off 설정인 notification(사용자당 1행)과는 다른 테이블이다.
    설정은 notification, 실제로 보낸 내역은 notification_log.

    대상은 종류마다 다르다. 여행 알림(추가·D-1·삭제)은 travel_idx, 열차 출발 10분 전
    알림(TRAIN_D10M)은 그 출발이 어디서 왔느냐에 따라 schedule_idx(추천 코스 승차권)
    또는 ticket_idx(직접 입력 승차권)를 채운다. 풍경 알림(SCENERY)은 그 둘에 더해
    scenic_spot_idx까지 채운다 — '탑승 1건에서 그 스팟은 한 번만'이 중복 판정 축이다.
    다형 참조(ref_type/ref_idx) 대신 대상별 컬럼을 따로 두는 이유는 FK로 무결성을 걸 수
    있고 부분 유니크 인덱스로 '한 번만 발송'을 DB가 강제할 수 있기 때문이다.

    **풍경 알림은 이력을 남기지만 알림 화면 목록에는 뜨지 않는다.** 이력을 남기는 건
    오로지 중복 발송을 막기 위해서다 — 발송 시각표를 어디에도 저장하지 않고 매 분
    다시 계산하는 구조라(scenic_plan_service), 같은 구간을 이미 보냈는지 아는 방법이
    이 테이블뿐이다. 화면에서 빼는 건 notification_service.list_logs가 맡는다.
    """

    __tablename__ = 'notification_log'
    __table_args__ = (
        # 알림 화면의 커서 페이징 질의(user_idx로 좁히고 PK 역순) 그대로를 태운다.
        # 정렬 키를 created_at이 아닌 PK로 잡는 이유는 dao.list_by_user 참조.
        Index('ix_notification_log_user_idx', 'user_idx', 'notification_log_idx'),
        # D-1은 여행당 1회라는 불변식을 DB로 강제한다. 자정 배치와 저장 시점 즉시 발송이
        # 동시에 같은 여행을 집으면 양쪽 다 이력 검사를 통과할 수 있어(check-then-act),
        # 애플리케이션 검사만으로는 중복 발송을 완전히 막지 못한다. 늦게 INSERT한 쪽이
        # 여기서 걸려 push_service.notify의 예외 처리로 롤백되고 푸시도 나가지 않는다.
        # 다른 종류(추가·삭제)는 반복될 수 있어 부분 인덱스로 D-1만 건다.
        Index(
            'uq_notification_log_travel_d1', 'user_idx', 'travel_idx',
            unique=True,
            postgresql_where=text("type = 'TRAVEL_D1'"),
            sqlite_where=text("type = 'TRAVEL_D1'"),
        ),
        # 열차 출발 10분 전 알림도 출발 1건당 1회다. 1분마다 도는 루프가 같은 열차를
        # 10번 집으므로(10분 창) 애플리케이션 검사만으론 경합에서 새고, D-1과 같은
        # 이유로 부분 유니크 인덱스가 최종 방어를 맡는다. 출처가 둘이라 인덱스도 둘이다
        # — 한 행은 schedule_idx/ticket_idx 중 하나만 채우므로 서로 안 겹친다
        # (NULL은 유니크 제약에서 서로 구별되어 반대쪽 인덱스에 걸리지 않는다).
        Index(
            'uq_notification_log_train_schedule', 'user_idx', 'schedule_idx',
            unique=True,
            postgresql_where=text("type = 'TRAIN_D10M'"),
            sqlite_where=text("type = 'TRAIN_D10M'"),
        ),
        Index(
            'uq_notification_log_train_ticket', 'user_idx', 'ticket_idx',
            unique=True,
            postgresql_where=text("type = 'TRAIN_D10M'"),
            sqlite_where=text("type = 'TRAIN_D10M'"),
        ),
        # 풍경 알림은 '탑승 1건에서 스팟 1개당 1회'다. 발송 시각표를 저장하지 않고 1분마다
        # 다시 계산하는 구조라, 같은 구간이 창(SEND_WINDOW_MINUTES) 안에 여러 번 잡힌다 —
        # 애플리케이션 검사만으론 경합에서 새므로 여기가 최종 방어다. 탑승 출처가 둘이라
        # 인덱스도 둘인 것은 TRAIN_D10M과 같다.
        Index(
            'uq_notification_log_scenery_schedule',
            'user_idx', 'schedule_idx', 'scenic_spot_idx',
            unique=True,
            postgresql_where=text("type = 'SCENERY'"),
            sqlite_where=text("type = 'SCENERY'"),
        ),
        Index(
            'uq_notification_log_scenery_ticket',
            'user_idx', 'ticket_idx', 'scenic_spot_idx',
            unique=True,
            postgresql_where=text("type = 'SCENERY'"),
            sqlite_where=text("type = 'SCENERY'"),
        ),
        {'comment': '발송된 알림 이력 (알림 화면 목록)'},
    )

    notification_log_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    user_idx = Column(
        Integer, ForeignKey('user.user_idx'), nullable=False, comment="FK 사용자"
    )
    type = Column(
        String(20), nullable=False,
        comment="TRAVEL_SAVED | TRAVEL_D1 | TRAVEL_DELETED | TRAIN_D10M | STAMP_EARNED | SCENERY",
    )
    title = Column(String(100), nullable=False, comment="알림 제목")
    body = Column(String(255), nullable=False, comment="알림 본문")
    travel_idx = Column(
        Integer, ForeignKey('travel.travel_idx'), nullable=True,
        comment="FK 여행 — 알림을 탭했을 때 열 여행이자 중복 발송 판정 키(D-1은 여행당 1회). "
                "여행과 무관한 알림이 생길 수 있어 nullable",
    )
    schedule_idx = Column(
        Integer, ForeignKey('schedule.schedule_idx'), nullable=True,
        comment="FK 일정 — TRAIN_D10M이 추천 코스 승차권(schedule kind=train)에서 나왔을 때. "
                "중복 발송 판정 키(출발 1건당 1회)",
    )
    ticket_idx = Column(
        Integer, ForeignKey('ticket.ticket_idx'), nullable=True,
        comment="FK 승차권 — TRAIN_D10M이 직접 입력 승차권(ticket)에서 나왔을 때. "
                "중복 발송 판정 키(출발 1건당 1회)",
    )
    scenic_spot_idx = Column(
        Integer, ForeignKey('scenic_spot.scenic_spot_idx'), nullable=True,
        comment="FK 관광지 — SCENERY가 어느 구간의 어느 스팟이었는지. schedule_idx/ticket_idx와 "
                "묶여 중복 발송 판정 키가 된다(탑승 1건에서 스팟 1개당 1회)",
    )
    # 스탬프 알림의 대상. 다른 대상들과 달리 FK가 아니라 종류 문자열이다 — 앱이 열 화면이
    # user_stamp 행 하나가 아니라 '스탬프 탭의 그 칸'이고, 칸은 PK가 아니라 종류로 식별된다.
    # 한 번만 발송하는 건 user_stamp의 (user_idx, type) 유니크가 이미 보장하므로
    # 여기엔 부분 유니크 인덱스를 걸지 않는다.
    stamp_type = Column(
        String(30), nullable=True,
        comment="스탬프 종류 (STAMP_EARNED일 때만, core.enums.StampType)",
    )
    read_at = Column(
        DateTime(timezone=True), nullable=True, comment="읽은 시각 (null이면 안 읽음)"
    )
