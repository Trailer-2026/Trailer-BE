from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from databases.database import get_db
from core.response import CommonResponse
from schemas.example_schema import ExampleCreateRequest, ExampleUpdateRequest
from services import example_service

router = APIRouter(prefix="/api/example", tags=["Example"])


@router.get("")
async def get_list(skip: int = 0, take: int = 10, db: Session = Depends(get_db)):
    result = example_service.get_list(skip, take, db)
    return CommonResponse.success_response("조회 성공", data=result)


@router.get("/{example_idx}")
async def get_detail(example_idx: int, db: Session = Depends(get_db)):
    result = example_service.get_detail(example_idx, db)
    return CommonResponse.success_response("조회 성공", data=result)


@router.post("")
async def create(request_data: ExampleCreateRequest, db: Session = Depends(get_db)):
    result = example_service.create(request_data.name, request_data.description, db)
    return CommonResponse.success_response("등록 성공", data=result)


@router.put("/{example_idx}")
async def update(example_idx: int, request_data: ExampleUpdateRequest, db: Session = Depends(get_db)):
    result = example_service.update(example_idx, request_data.name, request_data.description, db)
    return CommonResponse.success_response("수정 성공", data=result)


@router.delete("/{example_idx}")
async def delete(example_idx: int, db: Session = Depends(get_db)):
    example_service.delete(example_idx, db)
    return CommonResponse.success_response("삭제 성공")
