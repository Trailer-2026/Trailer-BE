from sqlalchemy import Column, Integer, String, Date, ForeignKey

from databases.models.base import BaseModel


class Travel(BaseModel):
    """저장된 여행(일정) — AI 추천 코스에서 사용자가 선택해 확정한 여행 1건.

    schedule(1:N)로 날짜·순서별 일정 항목을 갖는다. title은 선택한 플랜 제목이 기본이고
    사용자가 나중에 수정할 수 있다. status는 PLANNED→ONGOING→COMPLETED로 진행.
    """

    __tablename__ = "travel"
    __table_args__ = ({"comment": "일정(여행)"},)

    travel_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    user_idx = Column(
        Integer, ForeignKey("user.user_idx"), nullable=False, index=True, comment="FK 사용자"
    )
    title = Column(String(100), nullable=False, comment="여행 제목")
    start_date = Column(Date, nullable=False, comment="여행 시작일")
    end_date = Column(Date, nullable=False, comment="여행 종료일")
    region = Column(String(100), nullable=True, comment="대표 지역")
    status = Column(String(20), nullable=False, default="PLANNED", comment="PLANNED, ONGOING, COMPLETED")
    # 사용자가 직접 지정한 대표 사진. null이면 첫 일정(schedule)의 image_url을 썸네일로 쓴다 —
    # AI 추천 여행은 그 폴백만으로 사진이 있지만, 직접 만든 여행은 일정에 이미지가 없어 비어 보인다.
    cover_image_url = Column(String(255), nullable=True, comment="대표 사진 URL (사용자 지정)")
