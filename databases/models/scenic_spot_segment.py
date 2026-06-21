from sqlalchemy import Column, Integer, String, ForeignKey, Index
from databases.models.base import BaseModel


class ScenicSpotSegment(BaseModel):
    """관광지 1개가 '어느 역 구간에서, 창밖 어느 쪽으로' 보이는지를 나타내는 한 행(segment).

    한 관광지는 여러 역 구간에 걸쳐 보일 수 있어 ScenicSpot : ScenicSpotSegment = 1:N이다.
    예) 한강 관광지 → (서울역→용산역) 구간에선 오른쪽 창, (용산역→영등포역) 구간에선 왼쪽 창.

    - from_station/to_station: 이 관광지가 보이는 역 구간 (저장 기준 방향)
    - side_hint_forward: 저장 방향(from→to)으로 진행할 때 창밖 좌/우
    - side_hint_reverse: 반대 방향(to→from)으로 진행할 때 창밖 좌/우
    좌/우는 노선이 아니라 '역 구간 + 진행 방향'으로만 결정되는 기하 속성이다.
    """

    __tablename__ = 'scenic_spot_segment'
    __table_args__ = (
        Index('ix_scenic_spot_segment_stations', 'from_station', 'to_station'),
        {'comment': '관광지가 보이는 구간/방향'},
    )

    scenic_spot_segment_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    scenic_spot_idx = Column(
        Integer, ForeignKey('scenic_spot.scenic_spot_idx'),
        nullable=False, index=True, comment="FK 관광지"
    )
    from_station = Column(String(50), nullable=False, comment="시작역")
    to_station = Column(String(50), nullable=False, comment="종착역")
    side_hint_forward = Column(String(10), nullable=True, comment="정방향 좌우(left|right)")
    side_hint_reverse = Column(String(10), nullable=True, comment="역방향 좌우(left|right)")
