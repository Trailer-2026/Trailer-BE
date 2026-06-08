from typing import Generic, Optional, TypeVar
from pydantic import BaseModel

T = TypeVar("T")


class CommonResponse(BaseModel, Generic[T]):
    code: int
    message: str
    data: Optional[T] = None

    @classmethod
    def success_response(cls, message: str, data: Optional[T] = None):
        return cls(code=200, message=message, data=data)

    @classmethod
    def fail_response(cls, message: str, code: int = 400, data: Optional[T] = None):
        return cls(code=code, message=message, data=data)
