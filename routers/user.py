from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy.orm import Session

from databases.database import get_db
from databases.models.user import User
from core.response import CommonResponse
from core.security import get_current_user
from schemas.notification_schema import (
    NotificationResponse,
    NotificationUpdateRequest,
)
from schemas.stamp_schema import StampListResponse
from schemas.user_schema import ProfileResponse, NicknameUpdateRequest
from schemas.video_schema import MyReelsListResponse
from services import notification_service, stamp_service, user_service, video_service

router = APIRouter(prefix="/api/users", tags=["사용자"])


@router.get(
    "/me/profile",
    summary="내 프로필 조회",
    description="내 정보/프로필 설정 화면에 쓰는 통합 프로필을 반환합니다. 닉네임·이메일·프로필 "
                "사진과 연동된 소셜 계정 목록(현재 가입 provider 1건, 대표)을 함께 내려줍니다. "
                "(access token 인증 필요)",
    response_model=CommonResponse[ProfileResponse],
)
async def get_my_profile(current_user: User = Depends(get_current_user)):
    result = user_service.get_profile(current_user)
    return CommonResponse.success_response("조회 성공", data=result)


@router.patch(
    "/me/nickname",
    summary="닉네임 편집",
    description="닉네임을 변경합니다(1~20자). 변경 후 갱신된 프로필을 반환합니다. "
                "(access token 인증 필요)",
    response_model=CommonResponse[ProfileResponse],
)
def update_nickname(
    request_data: NicknameUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = user_service.update_nickname(db, current_user, request_data.nickname)
    return CommonResponse.success_response("닉네임 변경 성공", data=result)


@router.get(
    "/me/notifications",
    summary="알림 설정 조회",
    description="알림 설정 화면의 on/off 스위치 상태를 반환합니다 — 이벤트 알림, 기차역 풍경 알림. "
                "스위치는 이 둘뿐입니다. 열차 출발 10분 전 탑승 알림은 별도 스위치 없이 이벤트 "
                "알림(event_alarm)에 함께 묶여 있습니다. "
                "설정을 한 번도 바꾼 적 없는 사용자는 기본값(둘 다 수신)으로 만들어 반환합니다. "
                "(access token 인증 필요)\n\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[NotificationResponse],
)
def get_notification_settings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = notification_service.get_settings(db, current_user)
    return CommonResponse.success_response("알림 설정 조회 성공", data=result)


@router.patch(
    "/me/notifications",
    summary="알림 설정 편집",
    description="알림 수신 여부를 변경합니다. 보낸 항목만 바뀌고 안 보낸 항목은 그대로 유지되므로, "
                "스위치 하나만 토글할 때는 그 항목만 보내면 됩니다. 변경 후 갱신된 설정을 반환합니다. "
                "(access token 인증 필요)\n\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[NotificationResponse],
)
def update_notification_settings(
    request_data: NotificationUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = notification_service.update_settings(db, current_user, request_data)
    return CommonResponse.success_response("알림 설정 변경 성공", data=result)


@router.get(
    "/me/stamps",
    summary="내 스탬프 목록",
    description="마이페이지 '스탬프' 탭 데이터입니다. **미달성 칸까지 전부** 담아 내려주므로 "
                "응답 순서 그대로 그리드에 그리면 됩니다(앱이 정렬하지 않습니다). 잠긴 칸은 "
                "`achieved=false`이고, 그 위에 자물쇠 아이콘을 덮는 건 앱 몫입니다.\n\n"
                "- 상단 '스탬프 달성 현황 N개'는 `achieved_count`입니다.\n"
                "- `progress`/`goal`로 '10곳 중 3곳'처럼 진행도를 보여줄 수 있습니다.\n"
                "- 조건은 **다녀온 여행**(종료일이 지난 여행)을 기준으로 판정합니다. "
                "예정·진행 중인 여행은 아직 세지 않습니다.\n"
                "- `SCENERY_PHOTOS`(풍경 사진)는 촬영 기록을 받는 API가 아직 없어 **항상 "
                "잠금**입니다. 앱이 사진을 보내기 시작하면 그때 열립니다.\n"
                "- `AI_COURSE_DONE`은 `travel.source` 컬럼이 생긴 뒤 저장한 여행부터 세므로, "
                "그 전에 저장한 추천 여행은 반영되지 않습니다.\n\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[StampListResponse],
)
def get_my_stamps(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = stamp_service.list_stamps(db, current_user.user_idx)
    return CommonResponse.success_response("스탬프 목록 조회 성공", data=result)


@router.get(
    "/me/reels",
    summary="내가 올린 릴스 목록",
    description="마이페이지의 내가 올린 릴스를 최신순으로 반환합니다. 홈 피드 카드와 같은 필드 "
                "구성이라 같은 카드로 그리면 됩니다. 렌더가 아직 끝나지 않은 릴스는 재생도 "
                "썸네일도 안 되므로 목록에서 빠집니다. 다음 페이지는 응답의 next_cursor를 "
                "cursor로 넘겨 요청하고, next_cursor가 null이면 마지막 페이지입니다. "
                "(access token 인증 필요)\n\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[MyReelsListResponse],
)
def get_my_reels(
    limit: int = Query(20, ge=1, le=100, description="한 번에 가져올 개수 (1~100, 기본 20)"),
    cursor: int | None = Query(
        None, ge=1, description="이전 응답의 next_cursor. 첫 페이지는 생략합니다.",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = video_service.list_my_reels(db, current_user, limit, cursor)
    return CommonResponse.success_response("내가 올린 릴스 목록 조회 성공", data=result)


@router.get(
    "/me/reels/liked",
    summary="좋아요한 릴스 목록",
    description="마이페이지에 저장(하트)해 둔 릴스를 내가 좋아요를 누른 순서대로 반환합니다. "
                "별도의 북마크 기능은 없고 릴스 좋아요(POST/DELETE /api/reels/{reels_idx}/likes)가 "
                "곧 저장이라, 목록의 하트를 해제하면 이 목록에서도 빠집니다. 내가 차단한 "
                "사용자의 릴스와 삭제된 릴스는 포함되지 않습니다. 다음 페이지는 응답의 "
                "next_cursor를 cursor로 넘겨 요청하고, next_cursor가 null이면 마지막 "
                "페이지입니다. (access token 인증 필요)\n\n"
                "- 401: 인증 필요",
    response_model=CommonResponse[MyReelsListResponse],
)
def get_my_liked_reels(
    limit: int = Query(20, ge=1, le=100, description="한 번에 가져올 개수 (1~100, 기본 20)"),
    cursor: int | None = Query(
        None, ge=1, description="이전 응답의 next_cursor. 첫 페이지는 생략합니다.",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = video_service.list_liked_reels(db, current_user, limit, cursor)
    return CommonResponse.success_response("좋아요한 릴스 목록 조회 성공", data=result)


@router.patch(
    "/me/profile-image",
    summary="프로필 사진 편집",
    description="프로필 사진 이미지 파일을 업로드(multipart/form-data)해 프로필 사진을 변경합니다. "
                "이미지가 아니거나 10MB를 초과하면 400을 반환합니다. 변경 후 갱신된 프로필을 "
                "반환합니다. (access token 인증 필요)",
    response_model=CommonResponse[ProfileResponse],
)
def update_profile_image(
    image: UploadFile = File(..., description="프로필 사진 이미지 파일 (jpg/png/webp 등, 10MB 이하)"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = user_service.update_profile_image(
        db, current_user, image.file.read(), image.content_type, image.filename
    )
    return CommonResponse.success_response("프로필 사진 변경 성공", data=result)
