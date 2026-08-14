from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session

from core.response import CommonResponse
from core.security import get_current_user
from databases.database import get_db
from databases.models.user import User
from schemas.travel_schema import (
    HomeTravelCard,
    PastTravelListResponse,
    ScheduleCreateRequest,
    ScheduleUpdateRequest,
    TravelCoverImageResponse,
    TravelCreateRequest,
    TravelDetailResponse,
    TravelImagesResponse,
    TravelLikeResponse,
    TravelListResponse,
    TravelManualCreateRequest,
    TravelResponse,
    TravelScheduleItem,
    TravelTicketsResponse,
    TravelUpdateRequest,
)
from services import travel_like_service, travel_service
from utils.gcs import MAX_IMAGE_BYTES  # 업로드 상한 — 초과분을 다 읽지 않으려고 라우터에서도 쓴다

router = APIRouter(prefix="/api/travels", tags=["Travel"])


@router.post(
    "",
    summary="추천 코스 저장",
    description="추천 응답에서 선택한 플랜의 `plan_id`를 받아 내 여행으로 저장합니다. "
                "서버가 캐시에서 그 플랜(기차·방문지·숙소)을 꺼내 Travel + 일정(schedule)으로 저장하며, "
                "제목은 플랜 제목이 기본값입니다.\n\n"
                "- 400: plan_id 캐시 만료(추천 후 시간 초과·서버 재시작) → 다시 추천받아야 합니다.\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TravelResponse],
)
def create_travel(
    req: TravelCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.save_selected_plan(db, current_user, req.plan_id)
    return CommonResponse.success_response("여행 저장 성공", data=result)


@router.post(
    "/manual",
    summary="직접 일정 만들기",
    description="빈 여행(일정) 1건을 직접 생성합니다(제목·기간·지역). 일정 항목은 이후 "
                "`POST /{travel_idx}/schedules`로 개별 추가합니다.\n\n"
                "- **예정 여행은 1개만** 가질 수 있습니다. 종료되지 않은 여행이 이미 있으면 400입니다.\n"
                "- 400: 예정 여행이 이미 있음 / 종료일이 시작일보다 빠름\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TravelResponse],
)
def create_manual_travel(
    req: TravelManualCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.create_manual(db, current_user, req)
    return CommonResponse.success_response("여행 생성 성공", data=result)


@router.get(
    "",
    summary="전체 여행 목록 조회",
    description="로그인한 사용자의 여행을 예정·진행 중·지난 구분 없이 한 번에 반환합니다. "
                "카드마다 status(PLANNED | ONGOING | COMPLETED)가 담겨 프론트가 섹션을 나눌 수 있습니다. "
                "status는 여행 기간과 오늘(KST)로 계산됩니다 — 시작 전 PLANNED, 기간 내 ONGOING, 종료 후 COMPLETED. "
                "정렬은 아직 끝나지 않은 여행(진행 중·예정)이 임박한 순으로 먼저 오고, 그 뒤에 지난 여행이 "
                "최근 종료순으로 붙습니다. 카드 썸네일은 여행의 첫 일정 대표 이미지이고, liked는 내가 하트를 "
                "누른 여행인지 여부입니다. 여행이 없으면 빈 배열을 반환합니다.\n\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TravelListResponse],
)
def get_travels(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.all_travels(db, current_user)
    return CommonResponse.success_response("전체 여행 조회 성공", data=result)


@router.get(
    "/current",
    summary="홈 화면 여행 카드 조회",
    description="로그인한 사용자의 '지금/곧 떠나는 여행' 1건을 반환합니다(홈 화면 여행 카드용). "
                "진행 중(ONGOING) 여행을 우선하고, 없으면 가장 가까운 예정(PLANNED) 여행을, 둘 다 없으면 null을 반환합니다. "
                "status는 여행 기간과 오늘(KST)로 계산됩니다 — 시작 전 PLANNED, 기간 내 ONGOING, 종료 후 COMPLETED.\n\n"
                "- data=null: 진행 중·예정 여행이 없음(홈 기본 화면 표시)\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[HomeTravelCard | None],
)
def get_current_travel(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.current_travel(db, current_user)
    return CommonResponse.success_response("홈 여행 카드 조회 성공", data=result)


# 아래 "/{travel_idx}"보다 먼저 선언해야 한다 — 순서가 바뀌면 "past"가 int 경로에 걸려 422.
@router.get(
    "/past",
    summary="지난 여행 목록 조회",
    description="로그인한 사용자의 '지난 여행'(이미 종료된 여행)을 종료일 내림차순으로 반환합니다"
                "(여행기록 화면 지난 여행 섹션용). "
                "종료 여부는 여행 종료일과 오늘(KST)로 계산하며, 종료일이 오늘 이전인 여행만 담습니다 — "
                "진행 중·예정 여행은 포함되지 않아 status는 항상 COMPLETED입니다. "
                "카드 썸네일은 여행의 첫 일정 대표 이미지이고, liked는 내가 하트를 누른 여행인지 여부입니다. "
                "지난 여행이 없으면 빈 배열을 반환합니다.\n\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[PastTravelListResponse],
)
def get_past_travels(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.past_travels(db, current_user)
    return CommonResponse.success_response("지난 여행 조회 성공", data=result)


@router.get(
    "/{travel_idx}",
    summary="여행 일정표 상세 조회",
    description="여행 1건의 일정표를 일자별 타임라인으로 반환합니다(내 일정 > 일정표 탭). "
                "추천 코스 저장의 응답으로 받은 `travel_idx`를 파라미터로 이용합니다."
                "일정 항목을 day_no(DAY)로 묶고 각 일자의 항목은 sequence 오름차순으로 정렬합니다. "
                "각 일자의 날짜는 여행 시작일 + (day_no-1)로 계산하며, 기차 항목의 title은 "
                "'KTX 101 서울→부산' 형태입니다. status는 여행 기간과 오늘(KST)로 계산됩니다.\n\n"
                "- 404: 존재하지 않거나 본인 여행이 아님\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TravelDetailResponse],
)
def get_travel_detail(
    travel_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.travel_detail(db, current_user, travel_idx)
    return CommonResponse.success_response("여행 일정표 조회 성공", data=result)


@router.get(
    "/{travel_idx}/tickets",
    summary="승차권 조회",
    description="여행의 승차권 목록을 반환합니다(승차권 화면용). "
                "AI 추천 일정을 승인(추천 코스 저장)한 여행에서만 조회할 수 있습니다 — "
                "승인 시 발급받은 `travel_idx`를 파라미터로 이용합니다. "
                "승차권 1매 = 기차 일정 1건이며, 승차 일자·출발/도착역·출발/도착 시각·열차 등급/번호를 담습니다. "
                "좌석·호차·타는곳 번호 등 예매 정보는 제공하지 않습니다. 기차 일정이 없으면 빈 배열입니다.\n\n"
                "- 404: 승인(저장)된 일정이 아니거나 본인 여행이 아님\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TravelTicketsResponse],
)
def get_travel_tickets(
    travel_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.travel_tickets(db, current_user, travel_idx)
    return CommonResponse.success_response("승차권 조회 성공", data=result)


@router.patch(
    "/{travel_idx}",
    summary="여행 이름 변경",
    description="여행 제목을 변경합니다. 제목을 빈 값·공백으로 보내면 생성 때와 같이 지역·기간으로 "
                "자동 생성됩니다('부산 2박 3일 여행' 형태). 변경 후 갱신된 여행 요약을 반환합니다.\n\n"
                "- 404: 존재하지 않거나 본인 여행이 아님\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TravelResponse],
)
def rename_travel(
    travel_idx: int,
    req: TravelUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.rename_travel(db, current_user, travel_idx, req.title)
    return CommonResponse.success_response("여행 이름 변경 성공", data=result)


@router.patch(
    "/{travel_idx}/cover-image",
    summary="여행 대표 사진 지정·변경",
    description="여행 카드 썸네일로 쓸 대표 사진 이미지 파일을 업로드(multipart/form-data)해 "
                "지정합니다. 이미 지정돼 있으면 새 사진으로 교체하고 옛 사진은 저장소에서 "
                "삭제합니다.\n\n"
                "썸네일 우선순위는 **지정한 대표 사진 → 여행 첫 일정의 대표 이미지(AI 추천으로 "
                "저장한 여행) → 지역 기본 사진**입니다. 직접 만든 여행은 일정에 이미지가 없어 "
                "지역 기본 사진이 뜨는데, 이 API로 사용자 사진을 올리면 그걸 덮어씁니다. "
                "AI 추천으로 저장한 여행의 사진을 바꾸는 데도 씁니다.\n\n"
                "- 400: 이미지 파일이 아니거나 빈 파일, 10MB 초과\n"
                "- 404: 존재하지 않거나 본인 여행이 아님\n"
                "- 401: 인증 필요\n"
                "- 502: 이미지 저장소(GCS) 업로드 실패",
    response_model=CommonResponse[TravelCoverImageResponse],
)
def set_travel_cover_image(
    travel_idx: int,
    image: UploadFile = File(..., description="대표 사진 이미지 파일 (jpg/png/webp 등, 10MB 이하)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.set_cover_image(
        db, current_user, travel_idx,
        # 상한+1바이트까지만 읽는다 — 초과분을 통째로 메모리에 올리지 않고도 서비스의
        # 크기 검증(len > MAX_IMAGE_BYTES)이 400으로 거절한다.
        image.file.read(MAX_IMAGE_BYTES + 1), image.content_type, image.filename,
    )
    return CommonResponse.success_response("대표 사진 지정 성공", data=result)


@router.delete(
    "/{travel_idx}/cover-image",
    summary="여행 대표 사진 삭제",
    description="지정한 대표 사진을 해제하고 저장소에서도 지웁니다. 해제하면 썸네일은 원래 "
                "규칙(첫 일정의 대표 이미지 → 지역 기본 사진)으로 돌아가고, 응답의 "
                "`cover_image_url`에 그 복귀한 URL이 담깁니다. 지정된 사진이 없어도 "
                "성공(멱등)입니다.\n\n"
                "- 404: 존재하지 않거나 본인 여행이 아님\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TravelCoverImageResponse],
)
def delete_travel_cover_image(
    travel_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.delete_cover_image(db, current_user, travel_idx)
    return CommonResponse.success_response("대표 사진 삭제 성공", data=result)


@router.post(
    "/{travel_idx}/images",
    summary="여행 사진 추가 (여행 중 찍은 사진)",
    description="여행 중 찍은 사진들을 그 여행에 붙입니다(multipart/form-data, 한 번에 최대 "
                "20장). **`POST /api/videos/render/travel`이 이 사진들로 여행 영상을 만듭니다** "
                "— 사진을 하나도 안 붙인 여행은 지도 이동만 있는 영상이 됩니다.\n\n"
                "`schedule_idx`는 **선택**이고, 보통 안 보내면 됩니다.\n"
                "- **안 주면**: 서버가 **사진 EXIF의 GPS 좌표를 읽어 가장 가까운 일정에 자동으로 "
                "매핑**합니다. 앱은 여행과 사진만 보내면 되고, 영상에서 사진이 찍힌 자리에 뜹니다.\n"
                "- **주면**: 자동 매핑 없이 그 일정에 그대로 붙습니다(사용자가 일정을 직접 고른 화면).\n"
                "- **좌표를 못 구하면**: 일정 없이(`schedule_idx: null`) 저장됩니다 — 여행에 일정이 "
                "아직 없거나, 사진에 GPS가 없는 경우(메신저로 받은 사진은 GPS가 지워져 있습니다). "
                "이 사진들은 영상에서 **마지막 지점 뒤에 몰아서** 나옵니다.\n\n"
                "응답의 `schedule_idx`로 어디에 매핑됐는지 확인할 수 있습니다. 여행 상세에서는 "
                "매핑된 사진이 `days[].items[].images`, 매핑 안 된 사진이 최상단 `images`로 "
                "내려갑니다.\n\n"
                "여기 올린 사진 장수가 마이페이지 **`SCENERY_PHOTOS`(풍경 사진 10장) 스탬프**를 "
                "켭니다. 열 장째를 올리는 순간 스탬프가 적립되고, **이벤트 알림 수신이 켜져 "
                "있으면**(`GET /api/users/me/notifications`의 `event_alarm`) 획득 푸시도 "
                "함께 나갑니다. 꺼져 있으면 적립만 되고 푸시·알림 이력은 없습니다.\n\n"
                "- 400: 이미지 파일이 아니거나 빈 파일, 10MB 초과, 21장 이상\n"
                "- 404: 존재하지 않거나 본인 여행이 아님 / `schedule_idx`가 그 여행의 일정이 아님\n"
                "- 401: 인증 필요\n"
                "- 502: 이미지 저장소(GCS) 업로드 실패",
    response_model=CommonResponse[TravelImagesResponse],
)
def add_travel_images(
    travel_idx: int,
    images: list[UploadFile] = File(..., description="여행 사진 파일들 (jpg/png/webp 등, 장당 10MB 이하, 최대 20장)"),
    schedule_idx: int | None = Form(None, description="사진을 붙일 일정 항목 PK (비우면 사진 GPS로 가장 가까운 일정에 자동 매핑)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.add_images(
        db, current_user, travel_idx, schedule_idx,
        # 본문은 서비스가 한 장씩 읽어 쓰고 버린다 — 여기서 다 읽으면 20장 × 10MB를
        # 동시에 들게 된다. 장수 검증(400)도 서비스가 읽기 전에 먼저 한다.
        [(f.file, f.content_type, f.filename) for f in images],
    )
    return CommonResponse.success_response("여행 사진 추가 성공", data=result)


@router.delete(
    "/{travel_idx}/images/{image_idx}",
    summary="여행 사진 삭제",
    description="여행에 붙인 사진 1장을 지웁니다(저장소 객체까지 삭제, 복구 불가). "
                "`image_idx`는 사진 추가 응답이나 여행 상세의 `images`에 담겨 옵니다.\n\n"
                "- 404: 사진이 없거나 본인 여행의 사진이 아님\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[None],
)
def delete_travel_image(
    travel_idx: int,
    image_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    travel_service.delete_image(db, current_user, travel_idx, image_idx)
    return CommonResponse.success_response("여행 사진 삭제 성공")


@router.delete(
    "/{travel_idx}",
    summary="여행 삭제",
    description="여행 1건을 삭제합니다(소프트 삭제). 그 여행의 일정 항목도 함께 삭제됩니다. "
                "예정 여행을 삭제하면 '예정 여행은 1개만' 제약이 풀려 새 여행을 만들 수 있습니다.\n\n"
                "- 404: 존재하지 않거나 본인 여행이 아님\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[None],
)
def delete_travel(
    travel_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    travel_service.delete_travel(db, current_user, travel_idx)
    return CommonResponse.success_response("여행 삭제 성공")


@router.post(
    "/{travel_idx}/schedules",
    summary="일정 항목 추가",
    description="여행에 일정 항목 1건을 추가합니다(내 일정 > 직접 만들기). `kind`로 구분합니다.\n\n"
                "- **kind=visit(장소)**: `day_no`·`start_time`(방문 시각)·`title`·`latitude`·`longitude` 필수"
                "(장소 검색 결과에서 채워 전송). `end_time` 미지정 시 방문 시각과 동일 처리, `memo`·`image_url` 선택.\n"
                "- **kind=train(티켓)**: `dep_date`·`arr_date`(출발일·도착일)·`start_time`(출발)·`end_time`(도착)·"
                "`train_no`·`train_grade`·`dep_station`·`arr_station` 필수, `car_no`·`seat_no`·`memo` 선택. "
                "day_no는 출발일로 서버가 계산하고, 좌표는 출발역명으로 조회합니다.\n"
                "- `sequence`는 서버가 그날 마지막 뒤로 자동 배정합니다.\n\n"
                "- 404: 존재하지 않거나 본인 여행이 아님\n"
                "- 400: kind별 필수값 누락 / 여행 기간을 벗어난 일자·출발일 / 도착일<출발일 / 출발역 좌표 없음\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TravelScheduleItem],
)
def add_schedule(
    travel_idx: int,
    req: ScheduleCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.add_schedule(db, current_user, travel_idx, req)
    return CommonResponse.success_response("일정 항목 추가 성공", data=result)


@router.patch(
    "/{travel_idx}/schedules/{schedule_idx}",
    summary="일정 항목 편집",
    description="일정 항목 1건을 수정합니다. 보낸 필드만 반영되고 나머지는 그대로 유지됩니다. "
                "일자(day_no) 이동·종류(kind) 변경은 지원하지 않습니다.\n\n"
                "- 404: 항목이 없거나 본인 여행의 항목이 아님\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TravelScheduleItem],
)
def update_schedule(
    travel_idx: int,
    schedule_idx: int,
    req: ScheduleUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_service.update_schedule(db, current_user, travel_idx, schedule_idx, req)
    return CommonResponse.success_response("일정 항목 수정 성공", data=result)


@router.delete(
    "/{travel_idx}/schedules/{schedule_idx}",
    summary="일정 항목 삭제",
    description="일정 항목 1건을 삭제합니다(소프트 삭제).\n\n"
                "- 404: 항목이 없거나 본인 여행의 항목이 아님\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[None],
)
def delete_schedule(
    travel_idx: int,
    schedule_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    travel_service.delete_schedule(db, current_user, travel_idx, schedule_idx)
    return CommonResponse.success_response("일정 항목 삭제 성공")


@router.post(
    "/{travel_idx}/likes",
    summary="여행 좋아요",
    description="여행 1건에 좋아요(하트)를 누릅니다(여행기록 > 지난 여행 카드용). "
                "토글이 아니라서 이미 좋아요한 여행에 다시 요청해도 에러 없이 liked=true를 반환합니다(멱등). "
                "본인 여행에만 누를 수 있습니다.\n\n"
                "- 404: 존재하지 않거나 본인 여행이 아님\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TravelLikeResponse],
)
def like_travel(
    travel_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_like_service.like_travel(db, current_user, travel_idx)
    return CommonResponse.success_response("여행 좋아요 성공", data=result)


@router.delete(
    "/{travel_idx}/likes",
    summary="여행 좋아요 취소",
    description="여행 1건의 좋아요(하트)를 취소합니다. "
                "좋아요하지 않은 여행에 요청해도 에러 없이 liked=false를 반환합니다(멱등).\n\n"
                "- 404: 존재하지 않거나 본인 여행이 아님\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[TravelLikeResponse],
)
def unlike_travel(
    travel_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = travel_like_service.unlike_travel(db, current_user, travel_idx)
    return CommonResponse.success_response("여행 좋아요 취소 성공", data=result)
