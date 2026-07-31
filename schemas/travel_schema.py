from datetime import date, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DateType = date  # 'date' 필드명과 타입명이 겹칠 때 쓰는 별칭


class TravelCreateRequest(BaseModel):
    """추천 코스 저장 요청 — 선택한 플랜의 plan_id만 보낸다.

    plan_id는 추천 응답(Itinerary.plan_id)에 담겨 오며, 서버가 그 플랜을 캐시에서 꺼내
    그대로 저장한다(긴 코스 payload 재전송 불필요). 캐시 만료 시 재추천이 필요하다.
    """

    plan_id: str = Field(..., description="추천 응답 플랜 id (Itinerary.plan_id)")


class TravelManualCreateRequest(BaseModel):
    """직접 일정 만들기 — 빈 여행 1건 생성 요청 (일정 항목은 이후 개별 추가)."""

    title: str | None = Field(None, max_length=100, description="여행 제목(미입력·공백이면 지역·기간으로 자동 생성)", examples=["부산 여행"])
    start_date: date = Field(..., description="여행 시작일", examples=["2026-08-01"])
    end_date: date = Field(..., description="여행 종료일", examples=["2026-08-03"])
    region: str | None = Field(None, max_length=100, description="대표 지역(선택)", examples=["부산"])


class ScheduleCreateRequest(BaseModel):
    """일정 항목 추가 요청 (장소 방문 / 기차 티켓). kind로 구분.

    - kind=visit(장소): `day_no`·`start_time`(방문 시각) 필수, `end_time` 미지정 시 방문 시각과 동일
      처리(단일 시각). `title`·`latitude`·`longitude` 필수(장소 검색 결과에서 채워 보낸다).
    - kind=train(티켓): `dep_date`·`arr_date`(출발일·도착일)·`start_time`(출발)·`end_time`(도착)·
      `train_no`·`train_grade`·`dep_station`·`arr_station` 필수. day_no는 출발일로 서버가 계산한다.
      좌표는 서버가 출발역명으로 조회한다. `car_no`·`seat_no`·`memo` 선택.
    sequence는 그날 마지막 뒤로 서버가 자동 배정한다.
    """

    kind: Literal["visit", "train"] = Field(..., description="항목 종류", examples=["visit"])
    start_time: time = Field(..., description="방문(기차는 출발) 시각 HH:MM", examples=["10:00"])
    end_time: time | None = Field(None, description="종료(기차는 도착) 시각 HH:MM. visit 미지정 시 방문 시각과 동일, train 필수", examples=["12:00"])
    memo: str | None = Field(None, description="메모", examples=["점심 예약"])

    # kind=visit
    day_no: int | None = Field(None, ge=1, description="여행 일자 day1=1 (visit 필수)", examples=[1])
    title: str | None = Field(None, max_length=100, description="장소명 (visit 필수)", examples=["감천문화마을"])
    latitude: float | None = Field(None, description="위도 (visit 필수)", examples=[35.0975])
    longitude: float | None = Field(None, description="경도 (visit 필수)", examples=[129.0107])
    image_url: str | None = Field(None, max_length=255, description="대표 이미지 URL (visit, 선택)")

    # kind=train
    dep_date: date | None = Field(None, description="출발일 (train 필수, 여행 기간 내)", examples=["2026-08-01"])
    arr_date: date | None = Field(None, description="도착일 (선택 — day_no는 출발일 기준이라 값 자체는 사용하지 않음)", examples=["2026-08-01"])
    train_no: str | None = Field(None, max_length=10, description="열차번호 (train 필수)", examples=["101"])
    train_grade: str | None = Field(None, max_length=20, description="열차 등급 (train 필수)", examples=["KTX"])
    dep_station: str | None = Field(None, max_length=50, description="출발역명 (train 필수, '역' 없이)", examples=["서울"])
    arr_station: str | None = Field(None, max_length=50, description="도착역명 (train 필수, '역' 없이)", examples=["부산"])
    car_no: str | None = Field(None, max_length=10, description="호차 번호 (train, 선택)", examples=["9"])
    seat_no: str | None = Field(None, max_length=10, description="좌석 번호 (train, 선택)", examples=["7A"])


class ScheduleUpdateRequest(BaseModel):
    """일정 항목 편집 — 보낸 필드만 수정한다(미포함 필드는 그대로).

    day_no(일자 이동)·kind 변경은 지원하지 않는다. 시간·메모·좌석/호차·장소 좌표 등 표시 필드만 수정.
    """

    title: str | None = Field(None, max_length=100, description="장소명/일정명")
    start_time: time | None = Field(None, description="시작 시각 HH:MM")
    end_time: time | None = Field(None, description="종료 시각 HH:MM")
    memo: str | None = Field(None, description="메모")
    latitude: float | None = Field(None, description="위도 (visit)")
    longitude: float | None = Field(None, description="경도 (visit)")
    image_url: str | None = Field(None, max_length=255, description="대표 이미지 URL (visit)")
    train_no: str | None = Field(None, max_length=10, description="열차번호 (train)")
    train_grade: str | None = Field(None, max_length=20, description="열차 등급 (train)")
    dep_station: str | None = Field(None, max_length=50, description="출발역명 (train)")
    arr_station: str | None = Field(None, max_length=50, description="도착역명 (train)")
    car_no: str | None = Field(None, max_length=10, description="호차 번호 (train)")
    seat_no: str | None = Field(None, max_length=10, description="좌석 번호 (train)")


class TravelUpdateRequest(BaseModel):
    """여행 수정 요청 — 현재는 제목만 바꾼다(기간·지역 편집은 지원하지 않는다)."""

    title: str = Field(
        ..., max_length=100,
        description="바꿀 여행 제목(빈 값·공백이면 지역·기간으로 자동 생성)",
        examples=["부산 뚜벅이 여행"],
    )


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


class PastTravelCard(BaseModel):
    """여행기록 화면 '지난 여행' 목록의 카드 하나 — 이미 종료된 여행."""

    travel_idx: int = Field(..., description="여행 PK (카드 탭 시 상세 이동용)", examples=[12])
    title: str = Field(..., description="여행 제목", examples=["고성 여행"])
    start_date: date = Field(..., description="여행 시작일", examples=["2026-06-28"])
    end_date: date = Field(..., description="여행 종료일 (카드에 표시되는 날짜)", examples=["2026-06-30"])
    status: str = Field(
        ..., description="항상 COMPLETED (종료된 여행만 목록에 담긴다)", examples=["COMPLETED"],
    )
    cover_image_url: str | None = Field(
        None, description="카드 썸네일 — 여행 첫 일정 대표 이미지. 없으면 null",
        examples=["http://tong.visitkorea.or.kr/cms/resource/87/2754987_image2_1.jpg"],
    )
    liked: bool = Field(..., description="내가 좋아요(하트)를 누른 여행인지 여부", examples=[False])


class PastTravelListResponse(BaseModel):
    """지난 여행 목록 — 종료일 내림차순(최근 여행 먼저)."""

    travels: list[PastTravelCard] = Field(
        ..., description="지난 여행 카드 목록 (종료일 내림차순). 지난 여행이 없으면 빈 배열",
    )
    total: int = Field(..., description="지난 여행 총 건수", examples=[3])


class TravelCard(BaseModel):
    """전체 여행 목록의 카드 하나 — 예정·진행 중·지난 여행을 status로 구분한다.

    PastTravelCard와 필드는 같지만 status가 COMPLETED로 고정되지 않는다.
    """

    travel_idx: int = Field(..., description="여행 PK (카드 탭 시 상세 이동용)", examples=[12])
    title: str = Field(..., description="여행 제목", examples=["부산 2박 3일 여행"])
    start_date: date = Field(..., description="여행 시작일", examples=["2026-08-01"])
    end_date: date = Field(..., description="여행 종료일", examples=["2026-08-03"])
    status: str = Field(
        ..., description="PLANNED | ONGOING | COMPLETED (여행 기간·오늘 KST 기준 계산)",
        examples=["PLANNED"],
    )
    cover_image_url: str | None = Field(
        None, description="카드 썸네일 — 여행 첫 일정 대표 이미지. 없으면 null",
        examples=["http://tong.visitkorea.or.kr/cms/resource/87/2754987_image2_1.jpg"],
    )
    liked: bool = Field(..., description="내가 좋아요(하트)를 누른 여행인지 여부", examples=[False])


class TravelListResponse(BaseModel):
    """전체 여행 목록 — 진행 중·예정을 임박한 순으로 먼저, 그 뒤 지난 여행을 최근 종료순으로."""

    travels: list[TravelCard] = Field(
        ..., description="여행 카드 목록. 여행이 하나도 없으면 빈 배열",
    )
    total: int = Field(..., description="여행 총 건수", examples=[4])


class TravelLikeResponse(BaseModel):
    """여행 좋아요 결과 — 요청 후 그 여행의 좋아요 상태."""

    travel_idx: int = Field(..., description="여행 PK", examples=[12])
    liked: bool = Field(..., description="좋아요 상태 (POST 후 true, DELETE 후 false)", examples=[True])


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
    car_no: str | None = Field(None, description="호차 번호 (직접 입력 기차만, 아니면 null)", examples=["9"])
    seat_no: str | None = Field(None, description="좌석 번호 (직접 입력 기차만, 아니면 null)", examples=["7A"])
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
    car_no: str | None = Field(None, description="호차 번호 (직접 입력만, 아니면 null)", examples=["9"])
    seat_no: str | None = Field(None, description="좌석 번호 (직접 입력만, 아니면 null)", examples=["7A"])


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
