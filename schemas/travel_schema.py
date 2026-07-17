from datetime import date, time

from pydantic import BaseModel, ConfigDict, Field

DateType = date  # 'date' 필드명과 타입명이 겹칠 때 쓰는 별칭


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


class TravelScheduleItem(BaseModel):
    """일정표 타임라인의 한 항목 (기차/방문지/숙소 공통, schedule 1행)."""

    model_config = ConfigDict(from_attributes=True)  # schedule ORM 객체에서 바로 매핑

    schedule_idx: int = Field(..., description="일정 항목 PK", examples=[1])
    sequence: int = Field(..., description="그날의 n번째 일정 (0부터, 타임라인 정렬 순서)", examples=[0])
    kind: str = Field(..., description="항목 종류 (train | visit | lodging)", examples=["train"])
    title: str = Field(
        ..., description="장소명/일정명 (기차는 'KTX 101 서울→부산' 형태 표시용 폴백)",
        examples=["KTX 101 서울→부산"],
    )
    train_no: str | None = Field(None, description="열차번호 (kind=train만, 아니면 null)", examples=["101"])
    train_grade: str | None = Field(None, description="열차 등급 (kind=train만, 예: KTX)", examples=["KTX"])
    dep_station: str | None = Field(
        None, description="출발역명 (kind=train만, 접미사 '역' 없음 — 프론트가 '역 승차' 조합)",
        examples=["서울"],
    )
    arr_station: str | None = Field(
        None, description="도착역명 (kind=train만, 접미사 '역' 없음 — 프론트가 '역 하차' 조합)",
        examples=["부산"],
    )
    start_time: time = Field(..., description="시작 시각 (HH:MM:SS)", examples=["09:33:00"])
    end_time: time = Field(..., description="종료 시각 (HH:MM:SS)", examples=["12:53:00"])
    latitude: float = Field(
        ..., description="위도 (방문지·숙소는 자기 좌표, 기차는 출발역 좌표)", examples=[37.557863],
    )
    longitude: float = Field(..., description="경도", examples=[126.969468])
    image_url: str | None = Field(
        None, description="대표 이미지 URL. 기차 등 없으면 null",
        examples=["http://tong.visitkorea.or.kr/cms/resource/87/2754987_image2_1.jpg"],
    )
    memo: str | None = Field(None, description="메모. 없으면 null", examples=[None])


class TrainTicketResponse(BaseModel):
    """승차권 1매 — 승인(저장)된 여행의 기차 일정(schedule kind=train) 1건.

    승차권 화면에 실제로 그릴 수 있는 정보만 담는다. 타는곳 번호·호차·좌석번호·운임 등
    예매 정보는 서버가 보유하지 않아 제공하지 않는다.
    """

    schedule_idx: int = Field(..., description="일정 항목 PK", examples=[3])
    day_no: int = Field(..., description="여행 일자 (day1=1) — 'DAY 01' 표기용", examples=[1])
    date: DateType = Field(..., description="승차 일자 (여행 시작일 + day_no-1)", examples=["2026-07-03"])
    train_grade: str = Field(..., description="열차 등급", examples=["KTX"])
    train_no: str = Field(..., description="열차번호 (앞자리 0 제거)", examples=["111"])
    dep_station: str = Field(..., description="출발역명 (접미사 '역' 없음)", examples=["서울"])
    arr_station: str = Field(..., description="도착역명 (접미사 '역' 없음)", examples=["부산"])
    dep_time: time = Field(..., description="출발 시각 (HH:MM:SS)", examples=["08:00:00"])
    arr_time: time = Field(..., description="도착 시각 (HH:MM:SS)", examples=["14:00:00"])


class TravelTicketsResponse(BaseModel):
    """여행 1건의 승차권 목록 — 승인(저장)된 AI 추천 일정에서만 조회 가능."""

    travel_idx: int = Field(..., description="여행 PK")
    tickets: list[TrainTicketResponse] = Field(
        ..., description="승차권 목록 (day_no·sequence 오름차순). 기차 일정이 없으면 빈 배열",
    )


class TravelDayGroup(BaseModel):
    """여행 하루치 일정 묶음 — DAY 카드 하나."""

    day_no: int = Field(..., description="여행 일자 (day1=1)")
    date: DateType = Field(..., description="해당 일자 날짜 (여행 시작일 + day_no-1)")
    items: list[TravelScheduleItem] = Field(..., description="그날 일정 항목 (sequence 오름차순)")


class TravelDetailResponse(BaseModel):
    """여행 일정표 상세 — 일자별로 그룹된 타임라인."""

    travel_idx: int = Field(..., description="여행 PK")
    title: str = Field(..., description="여행 제목")
    start_date: date = Field(..., description="여행 시작일")
    end_date: date = Field(..., description="여행 종료일")
    region: str | None = Field(None, description="대표 지역. 없으면 null")
    status: str = Field(..., description="PLANNED | ONGOING | COMPLETED (여행 기간·오늘 KST 기준 계산)")
    days: list[TravelDayGroup] = Field(..., description="일자별 일정 묶음 (day_no 오름차순). 일정이 없으면 빈 배열")
