from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from databases.database import get_db
from databases.models.user import User
from core.response import CommonResponse
from core.security import get_current_user
from schemas.fcm_schema import FcmTokenRequest, TestPushRequest, PushResultResponse
from services import fcm_service

router = APIRouter(prefix="/api/fcm", tags=["FCM"])


@router.post(
    "/token",
    summary="FCM 토큰 등록",
    description="앱이 발급받은 FCM 기기 토큰을 현재 로그인한 사용자에 등록합니다. "
                "이미 등록된 토큰이면 소유 사용자를 갱신합니다. (access token 인증 필요)",
    response_model=CommonResponse[None],
)
def register_fcm_token(
    request_data: FcmTokenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    fcm_service.register_token(db, current_user.user_idx, request_data.token)
    return CommonResponse.success_response("FCM 토큰 등록 성공")


@router.post(
    "/test-send",
    summary="푸시 발송 테스트",
    description="지정한 사용자의 모든 기기로 테스트 푸시를 발송합니다. 등록된 토큰이 "
                "없으면 sent/failed 모두 0을 반환합니다. (개발용)",
    response_model=CommonResponse[PushResultResponse],
)
def test_send_push(
    request_data: TestPushRequest,
    db: Session = Depends(get_db),
):
    result = fcm_service.send_push(
        db, request_data.user_idx, request_data.title, request_data.body
    )
    return CommonResponse.success_response("푸시 발송 완료", data=result)
