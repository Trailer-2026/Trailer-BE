import os
import json
import logging

import firebase_admin
from firebase_admin import credentials, messaging

from config import parser

logger = logging.getLogger(__name__)


def init_firebase() -> None:
    """Firebase Admin SDK를 1회 초기화한다.

    - OPENAPI_EXPORT=1(노션 동기화 등 앱만 import하는 경우)에는 자격증명이 없을 수
      있으므로 건너뛴다.
    - --reload 등으로 중복 초기화되는 것을 _apps 가드로 막는다.
    - 서비스 계정 키는 properties_dev.ini [firebase] credentials 에 JSON 문자열로
      보관한다. URL의 %40 같은 값이 ConfigParser interpolation과 충돌하므로
      raw=True로 읽는다.
    """
    if os.getenv("OPENAPI_EXPORT") == "1":
        logger.info("OPENAPI_EXPORT=1 → Firebase 초기화 건너뜀")
        return
    if firebase_admin._apps:
        return

    raw = parser.get("firebase", "credentials", raw=True, fallback=None)
    if not raw:
        logger.warning("[firebase] credentials 설정이 없어 Firebase 초기화를 건너뜁니다.")
        return

    cred = credentials.Certificate(json.loads(raw))
    firebase_admin.initialize_app(cred)
    logger.info("Firebase 초기화 완료")


def send_multicast(tokens: list[str], title: str, body: str, data: dict = None):
    """여러 토큰으로 푸시를 발송하고 (성공 수, 실패 수, 죽은 토큰 목록)을 반환한다.

    죽은 토큰(UnregisteredError)은 호출 측이 정리할 수 있도록 목록으로 돌려준다.
    이 함수는 FCM 호출만 담당하며 DB는 건드리지 않는다.
    """
    if not tokens:
        return 0, 0, []

    message = messaging.MulticastMessage(
        notification=messaging.Notification(title=title, body=body),
        data={k: str(v) for k, v in (data or {}).items()},  # FCM data 값은 모두 문자열
        tokens=tokens,
    )
    resp = messaging.send_each_for_multicast(message)

    dead = [
        tokens[i]
        for i, r in enumerate(resp.responses)
        if not r.success and isinstance(r.exception, messaging.UnregisteredError)
    ]
    return resp.success_count, resp.failure_count, dead
