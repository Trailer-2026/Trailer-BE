# -*- coding: utf-8 -*-
from utils.logging_config import setup_logging
setup_logging()

import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from starlette.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from core.exceptions.base import AppException
from core.exceptions.handlers import (
    app_exception_handler,
    http_exception_handler,
    global_exception_handler,
    validation_exception_handler,
)
from databases.database import engine, SessionLocal
from databases.models.scenic_spot import ScenicSpot
from databases.models.scenic_spot_segment import ScenicSpotSegment
from services import scenic_spot_service

from routers.auth import router as auth_router
from routers.scenic_spot import router as scenic_spot_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 수명주기 훅: yield 이전=startup(요청 받기 직전), yield 이후=shutdown(종료 시).

    여기선 startup에서 관광지 시드를 1회 적재한다. 관광지 검색(search_on_segment)은
    DB 테이블을 읽으므로, 비어 있으면 결과가 항상 빈 배열이라 최초 1회 시드가 필요하다.
    """
    # OPENAPI_EXPORT=1 = 노션 동기화/OpenAPI 추출 모드. 이때는 인메모리 SQLite라
    # Postgres가 없어 시드를 돌리면 깨지므로 통째로 건너뛴다.
    if os.getenv("OPENAPI_EXPORT") != "1":
        try:
            # 관광 스팟 테이블이 없으면 생성(checkfirst=True → 이미 있으면 skip)해 존재만 보장
            ScenicSpot.__table__.create(engine, checkfirst=True)
            ScenicSpotSegment.__table__.create(engine, checkfirst=True)
            # lifespan은 요청이 아니라 Depends(get_db)를 못 쓴다 → 세션을 직접 열고 직접 닫는다
            db = SessionLocal()
            try:
                # 테이블이 비어 있을 때만 JSON 시드 적재 (이미 차 있으면 내부에서 즉시 return)
                scenic_spot_service.seed_if_empty(db)
            finally:
                db.close()  # 성공/실패 무관하게 세션 누수 방지
        except Exception:
            # 시드는 부가 데이터일 뿐 → 실패해도 로그만 남기고 서버 기동은 막지 않는다
            logger.exception("관광지 시드 초기화 실패")
    yield
    # (yield 이후 비어 있음: 종료 시 정리할 작업이 생기면 여기에 추가)


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_exception_handler(AppException, app_exception_handler)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, global_exception_handler)


app.include_router(auth_router)
app.include_router(scenic_spot_router)


@app.get("/")
async def root():
    return {"message": "Hello, World!"}
