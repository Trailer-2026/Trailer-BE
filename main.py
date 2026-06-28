# -*- coding: utf-8 -*-
from utils.logging_config import setup_logging
setup_logging()

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
from routers.auth import router as auth_router
from routers.scenic_spot import router as scenic_spot_router

logger = logging.getLogger(__name__)
from routers.fcm import router as fcm_router
from utils.firebase import init_firebase


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_firebase()
    yield


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


@app.get("/")
async def root():
    return {"message": "Hello, World!"}
