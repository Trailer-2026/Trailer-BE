from pydantic import BaseModel, Field

from core.enums import StampType


class StampItem(BaseModel):
    """스탬프 1칸. 잠금 여부와 진행도를 함께 담아 앱이 한 응답으로 화면을 그린다."""

    type: StampType = Field(
        ..., description="스탬프 종류(고정 키). 앱이 아이콘을 직접 갖고 있다면 이걸로 매칭한다",
        examples=["FIRST_TRAIN_TRIP"],
    )
    title: str = Field(..., description="칸 아래 라벨", examples=["첫 기차여행"])
    description: str = Field(
        ..., description="획득 조건 안내 문구(상세 시트·툴팁용)",
        examples=["기차가 포함된 여행을 한 번 다녀오면 찍혀요"],
    )
    image_url: str = Field(
        ..., description="달성 시 보여줄 스탬프 이미지 URL. 미달성 칸은 앱의 자물쇠 아이콘으로 덮는다",
        examples=["https://storage.googleapis.com/trailer-bucket/stamp/first_train_trip.png"],
    )
    achieved: bool = Field(..., description="달성 여부", examples=[True])
    progress: int = Field(
        ..., description="현재 진행 수치(goal에서 멈춘다). 달성이면 goal과 같다", examples=[1],
    )
    goal: int = Field(..., description="달성에 필요한 수치", examples=[1])


class StampListResponse(BaseModel):
    """마이페이지 '스탬프' 탭 전체 — 상단 '스탬프 달성 현황 N개'와 9칸 그리드."""

    achieved_count: int = Field(..., description="달성한 스탬프 수(상단 숫자)", examples=[2])
    total_count: int = Field(..., description="전체 스탬프 수", examples=[9])
    stamps: list[StampItem] = Field(
        ..., description="스탬프 목록. **응답 순서가 곧 화면에 찍히는 순서**라 앱이 정렬하지 않는다",
    )
