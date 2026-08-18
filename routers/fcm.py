from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from databases.database import get_db
from databases.models.user import User
from core.response import CommonResponse
from core.security import get_current_user
from schemas.fcm_schema import FcmTokenRequest
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

# 푸시 발송 테스트(POST /api/fcm/test-send)는 삭제했다 — 인증 없이 요청 본문의 user_idx로
# 임의 사용자에게 임의 문구를 쏠 수 있었다(응답의 sent 수로 계정 열거도 됐다). 개발용
# 확인은 /token 등록 후 실제 트리거(여행 저장 등)로 한다. 되살릴 거면 get_current_user를
# 걸고 대상은 current_user 고정 — 대상 user_idx를 본문으로 받는 형태로는 두지 마라.
