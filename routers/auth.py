from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from databases.database import get_db
from databases.models.user import User
from core.response import CommonResponse
from core.security import get_current_user
from schemas.auth_schema import (
    SocialLoginRequest,
    GoogleIdTokenRequest,
    RefreshRequest,
    TokenResponse,
    UserResponse,
)
from services import auth_service

router = APIRouter(prefix="/api/auth", tags=["인증"])


@router.post(
    "/login/google",
    summary="구글 로그인",
    description="프론트가 전달한 구글 id_token의 서명/aud/iss/만료를 검증한 뒤 가입/로그인하고 "
                "자체 JWT를 발급합니다. (google_client_id 설정 필요)",
    response_model=CommonResponse[TokenResponse],
)
async def login_google(request_data: GoogleIdTokenRequest, db: Session = Depends(get_db)):
    result = auth_service.google_id_token_login(request_data.id_token, db)
    return CommonResponse.success_response("로그인 성공", data=result)


@router.post(
    "/login/kakao",
    summary="카카오 소셜 로그인",
    description="프론트가 전달한 카카오 access_token으로 가입/로그인 후 자체 JWT를 발급합니다. "
                "유효하지 않거나 만료된 카카오 access_token이면 400을 반환합니다.",
    response_model=CommonResponse[TokenResponse],
)
async def login_kakao(request_data: SocialLoginRequest, db: Session = Depends(get_db)):
    result = await auth_service.social_login("kakao", request_data.access_token, db)
    return CommonResponse.success_response("로그인 성공", data=result)


@router.post(
    "/refresh",
    summary="토큰 재발급",
    description="refresh token으로 새로운 access/refresh token을 발급합니다.",
    response_model=CommonResponse[TokenResponse],
)
async def refresh(request_data: RefreshRequest, db: Session = Depends(get_db)):
    result = auth_service.refresh_token(request_data.refresh_token, db)
    return CommonResponse.success_response("재발급 성공", data=result)


@router.post(
    "/logout",
    summary="로그아웃",
    description="전달한 refresh token을 무효화합니다. (이미 만료/무효인 토큰이어도 성공 처리)",
    response_model=CommonResponse[None],
)
async def logout(request_data: RefreshRequest, db: Session = Depends(get_db)):
    auth_service.logout(request_data.refresh_token, db)
    return CommonResponse.success_response("로그아웃 성공")


@router.post(
    "/logout-all",
    summary="모든 기기 로그아웃",
    description="현재 사용자에게 발급된 모든 refresh token을 무효화합니다. (access token 인증 필요)",
    response_model=CommonResponse[None],
)
async def logout_all(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    auth_service.logout_all(current_user.user_idx, db)
    return CommonResponse.success_response("모든 기기 로그아웃 성공")


@router.get(
    "/me",
    summary="내 정보 조회",
    description="access token으로 인증된 현재 사용자 정보를 조회합니다.",
    response_model=CommonResponse[UserResponse],
)
async def get_me(current_user: User = Depends(get_current_user)):
    result = UserResponse.model_validate(current_user)
    return CommonResponse.success_response("조회 성공", data=result)
