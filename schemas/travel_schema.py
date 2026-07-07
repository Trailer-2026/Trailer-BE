from datetime import date

from pydantic import BaseModel, Field


class TravelCreateRequest(BaseModel):
    """추천 코스 저장 요청 — 선택한 플랜의 plan_id만 보낸다.

    plan_id는 추천 응답(Itinerary.plan_id)에 담겨 오며, 서버가 그 플랜을 캐시에서 꺼내
    그대로 저장한다(긴 코스 payload 재전송 불필요). 캐시 만료 시 재추천이 필요하다.
    """

    plan_id: str = Field(..., description="추천 응답 플랜 id (Itinerary.plan_id)")


class TravelResponse(BaseModel):
    """저장된 여행 요약."""

    travel_idx: int = Field(..., description="여행 PK")
    title: str = Field(..., description="여행 제목")
    start_date: date = Field(..., description="여행 시작일")
    end_date: date = Field(..., description="여행 종료일")
    region: str | None = Field(None, description="대표 지역")
    status: str = Field(..., description="PLANNED | ONGOING | COMPLETED")
    schedule_count: int = Field(..., description="저장된 일정 항목 수")


class HomeTravelCard(BaseModel):
    """홈 화면 '여행 카드'에 실제 보이는 필드만 담은 응답."""

    travel_idx: int = Field(..., description="여행 PK (카드 탭 시 상세 이동용)")
    title: str = Field(..., description="여행 제목")
    start_date: date = Field(..., description="여행 시작일")
    end_date: date = Field(..., description="여행 종료일")
    status: str = Field(..., description="PLANNED | ONGOING | COMPLETED (여행 기간·오늘 KST 기준 계산)")
    cover_image_url: str | None = Field(None, description="카드 썸네일 — 여행 첫 일정 대표 이미지. 없으면 null")
