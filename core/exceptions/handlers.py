from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from core.response import CommonResponse
from core.exceptions.base import AppException
import traceback
from utils.discord import send_discord_alarm, DiscordMessage, Embed
from config.external import settings, is_production

async def app_exception_handler(request: Request, exc: AppException):
    return JSONResponse(
        status_code=exc.status_code,
        content=CommonResponse.fail_response(
            message=exc.message,
            code=exc.status_code
        ).model_dump()
    )


async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=CommonResponse.fail_response(
            message=exc.detail,
            code=exc.status_code
        ).model_dump()
    )


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


async def general_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content=CommonResponse.fail_response(
            message="서버 내부 오류가 발생했습니다.",
            code=500
        ).model_dump()
    )


async def global_exception_handler(request: Request, exc: Exception):
    error_traceback = traceback.format_exc()

    if is_production() and DISCORD_WEBHOOK_URL:
        message = DiscordMessage(
            content="🚨 **운영 서버 에러 발생**",
            embeds=[
                Embed(
                    title=f"Error: {type(exc).__name__}",
                    description=f"**Path:** {request.url.path}\n**Detail:** {str(exc)}\n\n**Traceback:**\n```python\n{error_traceback[:800]}```"
                )
            ]
        )
        await send_discord_alarm(DISCORD_WEBHOOK_URL, message)

    # 로컬이거나 전송 실패 시 터미널 출력
    print(error_traceback)

    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error"},
    )