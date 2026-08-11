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
from routers.ticket import router as ticket_router
from databases import provision
from utils.firebase import init_firebase

logger = logging.getLogger(__name__)


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


async def _stamp_daily_loop():
    """매일 KST 자정에 '어제 여행이 끝난' 사용자의 스탬프를 재판정한다.

    스탬프 대부분은 다녀와야 켜지는데 그건 사용자의 행동이 아니라 날짜가 지나서 일어나는
    일이다. 이 루프가 없으면 사용자가 스탬프 탭을 열기 전까지 알림이 나가지 않는다.

    시작 시 1회 보정 실행 — 자정에 서버가 꺼져 있었어도 놓친 판정을 채운다. 여기서만
    CATCHUP_DAYS만큼 거슬러 본다(며칠씩 꺼져 있었으면 어제 하루로는 못 잡는다). 이미 찍힌
    스탬프는 user_stamp 유니크 제약에 걸러지므로 몇 번 돌아도 중복 알림이 없다.
    """
    from services import stamp_service, trip_reminder_service

    log = logging.getLogger(__name__)
    try:
        n = await asyncio.to_thread(
            stamp_service.award_for_finished_travels, stamp_service.CATCHUP_DAYS,
        )
        if n:
            log.info("스탬프 시작 보정 발급: %d건", n)
    except Exception as e:
        log.warning("스탬프 시작 보정 실패(다음 자정에 재시도): %s", e)
    while True:
        try:
            await asyncio.sleep(trip_reminder_service.seconds_until_next_midnight_kst())
            n = await asyncio.to_thread(stamp_service.award_for_finished_travels)
            log.info("스탬프 일일 발급: %d건", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("스탬프 일일 발급 실패(다음 자정에 재시도): %s", e)


async def _train_departure_loop():
    """1분마다 '열차가 10분 뒤 출발해요' 알림을 보낸다 (추천 코스·직접 입력 승차권 공통).

    D-1(자정 1회)과 달리 주기가 짧다 — 10분 창을 놓치지 않으려면 그보다 촘촘히 돌아야 한다.
    출발 1건당 1회라는 건 notification_log가 보장하므로, 창이 겹쳐 같은 열차를 여러 번
    집어도 알림은 한 번만 나간다. 실패해도 루프는 유지되고 다음 분에 다시 시도한다.
    """
    from services import train_departure_service

    log = logging.getLogger(__name__)
    while True:
        try:
            n = await asyncio.to_thread(train_departure_service.send_departure_reminders)
            if n:
                log.info("열차 탑승 알림 발송: %d건", n)
            await asyncio.sleep(train_departure_service.CHECK_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("열차 탑승 알림 실패(다음 주기 재시도): %s", e)
            await asyncio.sleep(train_departure_service.CHECK_INTERVAL_SEC)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_firebase()
    # OPENAPI_EXPORT(노션 동기화·인메모리 DB)나 명시적 off일 땐 자동 갱신 비활성.
    # 다중 워커로 띄우면 워커마다 돌므로, 그 땐 off하고 cron/systemd timer로 스크립트를 돌려라.
    tasks = []
    if os.getenv("OPENAPI_EXPORT") != "1":
        # 마이그레이션 도구가 없어 테이블·컬럼·인덱스를 앱이 직접 챙긴다. 단계 사이의
        # 의존 순서(테이블 → 컬럼 → 인덱스)와 FK 순서는 provision 안에서 결정된다.
        provision.run()
        if os.getenv("TRAIN_STOP_AUTOSYNC", "1") == "1":
            tasks.append(asyncio.create_task(_train_stop_daily_loop()))
        if os.getenv("TRIP_REMINDER_AUTOSYNC", "1") == "1":
            tasks.append(asyncio.create_task(_trip_reminder_daily_loop()))
        if os.getenv("TRAIN_DEPARTURE_AUTOSYNC", "1") == "1":
            tasks.append(asyncio.create_task(_train_departure_loop()))
        if os.getenv("STAMP_AUTOSYNC", "1") == "1":
            tasks.append(asyncio.create_task(_stamp_daily_loop()))
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
app.include_router(ticket_router)
app.include_router(share_router)  # /r/{reels_idx} — 브라우저용 공유 페이지(HTML)

@app.get("/")
async def root():
    return {"message": "Hello, World!"}
