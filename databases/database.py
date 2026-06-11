import os  # os 추가
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import Config

logger = logging.getLogger(__name__)

# 노션 동기화(sync_notion.py) 실행 중일 때는 DB 연결 정보가 없어도 되므로 예외 처리를 합니다.
if os.getenv("OPENAPI_EXPORT") == "1":
    SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"  # 메모리 내 임시 DB 사용
else:
    try:
        SQLALCHEMY_DATABASE_URL = Config.read('app', 'db.url')
    except Exception as e:
        # 혹시나 설정 파일이나 섹션이 없을 경우를 대비해 한번 더 방어
        logger.warning(f"설정 파일을 읽을 수 없어 더미 DB URL을 사용합니다: {e}")
        SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"

# DB 엔진 설정 (SQLite와 MySQL은 connect_args가 다를 수 있어 조건부 처리)
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine = create_engine(SQLALCHEMY_DATABASE_URL)
else:
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=20,
        max_overflow=10,
        connect_args={"charset": "utf8mb4"}
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()