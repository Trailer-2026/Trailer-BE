from sqlalchemy import Column, ForeignKey, Integer, String, UniqueConstraint

from databases.models.base import BaseModel


class UserStamp(BaseModel):
    """획득한 스탬프 1건 — 마이페이지 스탬프 탭의 '켜진 칸'.

    조건 판정은 여행·일정·릴스를 세서 하지만(services/stamp_service), **켜졌다는 사실은
    여기 남긴다.** 획득 순간에 알림을 보내려면 '언제 처음 켜졌는지'를 서버가 알아야 하고,
    조회할 때마다 다시 세기만 해서는 그 순간을 붙잡을 수 없기 때문이다.

    한 번 딴 스탬프는 조건이 나중에 어긋나도(여행을 지우는 등) 유지된다 — 행을 지우지
    않는다. 획득 시각은 BaseModel의 created_at이다(별도 컬럼을 두지 않는다).

    (user_idx, type) 유니크가 '한 번만'의 최종 방어다. 자정 배치와 스탬프 탭 열기가 같은
    순간에 같은 스탬프를 적립하려 들 수 있는데, 늦게 INSERT한 쪽이 여기서 걸려 알림도
    한 번만 나간다.
    """

    __tablename__ = "user_stamp"
    __table_args__ = (
        UniqueConstraint("user_idx", "type", name="uq_user_stamp"),
        {"comment": "획득한 스탬프"},
    )

    user_stamp_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    user_idx = Column(
        Integer, ForeignKey("user.user_idx"), nullable=False, index=True, comment="FK 사용자"
    )
    type = Column(String(30), nullable=False, comment="스탬프 종류 (core.enums.StampType)")
