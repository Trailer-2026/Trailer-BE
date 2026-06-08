import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import Config

logger = logging.getLogger(__name__)

SQLALCHEMY_DATABASE_URL = Config.read('app', 'db.url')

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
