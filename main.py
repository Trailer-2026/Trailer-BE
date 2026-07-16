# -*- coding: utf-8 -*-
from utils.logging_config import setup_logging
setup_logging()

import asyncio
import logging
import os

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
from routers.auth import router as auth_router
from routers.scenic_spot import router as scenic_spot_router

logger = logging.getLogger(__name__)
from routers.fcm import router as fcm_router
from routers.station import router as station_router
from routers.recommend import router as recommend_router
from routers.travel import router as travel_router
from routers.place import router as place_router
from routers.comment import router as comment_router
from routers.like import router as like_router
from routers.ban import router as ban_router
from utils.firebase import init_firebase


async def _train_stop_daily_loop():
    """서버가 켜져 있는 동안 하루 한 번 열차 정차역(train_stop)을 자동 갱신한다.

    시작 시엔 데이터가 없거나 오래됐을 때만 즉시 1회(개발 --reload 재시작마다 API 호출 방지),
    이후 24h 주기로 갱신. 실패해도 루프는 유지되고 기존 데이터로 서비스는 계속된다.
    네트워크·DB 작업은 blocking이라 to_thread로 이벤트 루프를 막지 않는다.
    """
    from services import train_stop_service

    log = logging.getLogger(__name__)
    try:
        n = await asyncio.to_thread(train_stop_service.refresh_if_stale)
        if n:
            log.info("train_stop 시작 갱신: %d행", n)
    except Exception as e:
        log.warning("train_stop 시작 갱신 실패(기존 데이터로 진행): %s", e)
    while True:
        try:
            await asyncio.sleep(train_stop_service.REFRESH_INTERVAL_SEC)
            n = await asyncio.to_thread(train_stop_service.refresh)
            log.info("train_stop 일일 갱신: %d행", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("train_stop 일일 갱신 실패(다음 주기 재시도): %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_firebase()
    # OPENAPI_EXPORT(노션 동기화·인메모리 DB)나 명시적 off일 땐 자동 갱신 비활성.
    # 다중 워커로 띄우면 워커마다 돌므로, 그 땐 off하고 cron/systemd timer로 스크립트를 돌려라.
    task = None
    if os.getenv("OPENAPI_EXPORT") != "1" and os.getenv("TRAIN_STOP_AUTOSYNC", "1") == "1":
        task = asyncio.create_task(_train_stop_daily_loop())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task  # 취소가 실제로 반영돼 진행 중인 갱신이 멈출 때까지 대기
            except asyncio.CancelledError:
                pass


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
app.include_router(fcm_router)
app.include_router(station_router)
app.include_router(recommend_router)
app.include_router(travel_router)
app.include_router(place_router)
app.include_router(comment_router)
app.include_router(like_router)
app.include_router(ban_router)

@app.get("/")
async def root():
    return {"message": "Hello, World!"}
