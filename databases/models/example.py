from sqlalchemy import Column, Integer, String
from databases.models.base import BaseModel


class Example(BaseModel):
    __tablename__ = 'example'
    __table_args__ = {'comment': '예시 테이블'}

    example_idx = Column(Integer, primary_key=True, autoincrement=True, comment="PK")
    name = Column(String(100), nullable=False, comment="이름")
    description = Column(String(500), nullable=True, comment="설명")
