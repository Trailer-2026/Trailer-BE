from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from databases.database import get_db
from core.response import CommonResponse
from schemas.example_schema import ExampleCreateRequest, ExampleUpdateRequest
from services import example_service

# tags는 Swagger에서 API들을 그룹화해줍니다.
router = APIRouter(prefix="/api/example", tags=["Example 관리"])

@router.get(
    "/test",
    summary="500 에러 테스트",
    description="강제로 ZeroDivisionError를 발생시켜 디스코드 알림이 오는지 테스트합니다."
)
async def test():
    return 1 / 0


@router.get(
    "",
    summary="예제 리스트 조회",
    description="페이징 처리가 된 예제 리스트를 조회합니다.",
    responses={200: {"description": "성공적으로 리스트를 가져왔을 때"}}
)
async def get_list(skip: int = 0, take: int = 10, db: Session = Depends(get_db)):
    result = example_service.get_list(skip, take, db)
    return CommonResponse.success_response("조회 성공", data=result)


@router.get(
    "/{example_idx}",
    summary="예제 상세 조회",
    description="특정 ID(index)를 가진 예제의 상세 정보를 조회합니다."
)
async def get_detail(example_idx: int, db: Session = Depends(get_db)):
    result = example_service.get_detail(example_idx, db)
    return CommonResponse.success_response("조회 성공", data=result)


@router.post(
    "",
    summary="예제 생성",
    description="새로운 예제 데이터를 등록합니다.",
    status_code=status.HTTP_201_CREATED
)
async def create(request_data: ExampleCreateRequest, db: Session = Depends(get_db)):
    result = example_service.create(request_data.name, request_data.description, db)
    return CommonResponse.success_response("등록 성공", data=result)


@router.put(
    "/{example_idx}",
    summary="예제 수정",
    description="특정 ID를 가진 예제의 이름과 설명을 수정합니다."
)
async def update(example_idx: int, request_data: ExampleUpdateRequest, db: Session = Depends(get_db)):
    result = example_service.update(example_idx, request_data.name, request_data.description, db)
    return CommonResponse.success_response("수정 성공", data=result)


@router.delete(
    "/{example_idx}",
    summary="예제 삭제",
    description="특정 ID를 가진 예제 데이터를 물리적으로 삭제합니다."
)
async def delete(example_idx: int, db: Session = Depends(get_db)):
    example_service.delete(example_idx, db)
    return CommonResponse.success_response("삭제 성공")