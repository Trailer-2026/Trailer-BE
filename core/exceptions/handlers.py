import traceback
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from core.response import CommonResponse
from core.exceptions.base import AppException
from utils.discord import send_discord_alarm, DiscordMessage, Embed
from config.external import DISCORD_WEBHOOK_URL, is_production

# 1. 커스텀 예외 핸들러
async def app_exception_handler(request: Request, exc: AppException):
    return JSONResponse(
        status_code=exc.status_code,
        content=CommonResponse.fail_response(
            message=exc.message,
            code=exc.status_code
        ).model_dump()
    )

# 2. HTTP 예외 핸들러
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=CommonResponse.fail_response(
            message=exc.detail,
            code=exc.status_code
        ).model_dump()
    )

# 3. 유효성 검사 예외 핸들러
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    if errors:
        first_error = errors[0]
        field = first_error.get("loc", ["", ""])[-1]
        message = f"{field}: {first_error.get('msg', '유효하지 않은 값입니다.')}"
    else:
        message = "잘못된 요청입니다."

    return JSONResponse(
        status_code=422,
        content=CommonResponse.fail_response(
            message=message,
            code=422
        ).model_dump()
    )

# 4. 전역 500 에러 핸들러 (디스코드 알림 포함)
async def general_exception_handler(request: Request, exc: Exception):
    error_traceback = traceback.format_exc()

    # 운영 환경일 때만 디스코드 전송
    if is_production() and DISCORD_WEBHOOK_URL:
        try:
            message = DiscordMessage(
                content="🚨 **운영 서버 500 에러 발생**",
                embeds=[
                    Embed(
                        title=f"Error: {type(exc).__name__}",
                        description=(
                            f"**Path:** {request.method} {request.url.path}\n"
                            f"**Detail:** {str(exc)}\n\n"
                            f"**Traceback:**\n```python\n{error_traceback[:800]}```"
                        )
                    )
                ]
            )
            await send_discord_alarm(DISCORD_WEBHOOK_URL, message)
        except Exception as discord_err:
            print(f"Failed to send discord alarm: {discord_err}")

    # 로컬 콘솔에 에러 출력
    print(error_traceback)

    # 응답은 기존 프로젝트의 fail_response 형식에 맞춤
    return JSONResponse(
        status_code=500,
        content=CommonResponse.fail_response(
            message="서버 내부 오류가 발생했습니다. 관리자에게 문의하세요.",
            code=500
        ).model_dump()
    )