from fastapi import status
from core.exceptions.base import AppException


class NotFoundException(AppException):
    def __init__(self, message: str = "리소스를 찾을 수 없습니다."):
        super().__init__(message=message, status_code=status.HTTP_404_NOT_FOUND)


class BadRequestException(AppException):
    def __init__(self, message: str = "잘못된 요청입니다."):
        super().__init__(message=message, status_code=status.HTTP_400_BAD_REQUEST)


class UnauthorizedException(AppException):
    def __init__(self, message: str = "인증이 필요합니다."):
        super().__init__(message=message, status_code=status.HTTP_401_UNAUTHORIZED)


class ExternalServiceException(AppException):
    def __init__(self, message: str = "외부 서비스 호출에 실패했습니다."):
        super().__init__(message=message, status_code=status.HTTP_502_BAD_GATEWAY)
