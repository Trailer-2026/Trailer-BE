from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from databases.database import get_db
from databases.models.user import User
from core.response import CommonResponse
from core.security import get_current_user
from schemas.notification_schema import (
    NotificationResponse,
    NotificationUpdateRequest,
)
from schemas.user_schema import ProfileResponse, NicknameUpdateRequest
from services import notification_service, user_service

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
