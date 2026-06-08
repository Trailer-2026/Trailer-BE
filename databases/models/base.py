from sqlalchemy import Column, DateTime
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class BaseModel(Base):
    __abstract__ = True

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), comment="생성일"
    )
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), comment="수정일")
    deleted_at = Column(DateTime(timezone=True), nullable=True, comment="삭제일")
