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
from routers.video import router as video_router
from routers.user import router as user_router
from routers.share import router as share_router
from routers.notification import router as notification_router
from services import push_service, video_service
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


async def _trip_reminder_daily_loop():
    """매일 KST 자정에 '여행이 하루 남았습니다'(D-1) 알림을 보낸다.

    시작 시 1회 보정 실행 — 서버가 자정에 꺼져 있었어도 놓친 D-1을 채운다. 이미 보낸
    건은 notification_log로 걸러지므로 몇 번 돌아도 중복 발송되지 않는다.
    실패해도 루프는 유지되고 다음 날 다시 시도한다.
    """
    from services import trip_reminder_service

    log = logging.getLogger(__name__)
    try:
        n = await asyncio.to_thread(trip_reminder_service.send_d1_reminders)
        if n:
            log.info("D-1 여행 알림 시작 보정 발송: %d건", n)
    except Exception as e:
        log.warning("D-1 여행 알림 시작 보정 실패(다음 자정에 재시도): %s", e)
    while True:
        try:
            await asyncio.sleep(trip_reminder_service.seconds_until_next_midnight_kst())
            n = await asyncio.to_thread(trip_reminder_service.send_d1_reminders)
            log.info("D-1 여행 알림 발송: %d건", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("D-1 여행 알림 실패(다음 자정에 재시도): %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_firebase()
    # OPENAPI_EXPORT(노션 동기화·인메모리 DB)나 명시적 off일 땐 자동 갱신 비활성.
    # 다중 워커로 띄우면 워커마다 돌므로, 그 땐 off하고 cron/systemd timer로 스크립트를 돌려라.
    tasks = []
    if os.getenv("OPENAPI_EXPORT") != "1":
        push_service.ensure_tables()  # notification·notification_log 자체 provision (마이그레이션 도구 없음)
        video_service.ensure_reels_columns()  # reels.region·thumbnail_url 자체 provision (동상)
        if os.getenv("TRAIN_STOP_AUTOSYNC", "1") == "1":
            tasks.append(asyncio.create_task(_train_stop_daily_loop()))
        if os.getenv("TRIP_REMINDER_AUTOSYNC", "1") == "1":
            tasks.append(asyncio.create_task(_trip_reminder_daily_loop()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task  # 취소가 실제로 반영돼 진행 중인 작업이 멈출 때까지 대기
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
app.include_router(video_router)
app.include_router(user_router)
app.include_router(notification_router)
app.include_router(share_router)  # /r/{reels_idx} — 브라우저용 공유 페이지(HTML)

@app.get("/")
async def root():
    return {"message": "Hello, World!"}
