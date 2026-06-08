from typing import Optional, List
from pydantic import BaseModel


class ExampleCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None


class ExampleUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class ExampleResponse(BaseModel):
    example_idx: int
    name: str
    description: Optional[str] = None

    class Config:
        from_attributes = True


class ExampleListResponse(BaseModel):
    items: List[ExampleResponse]
    total_count: int
