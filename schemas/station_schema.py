from pydantic import BaseModel, ConfigDict, Field


class StationResponse(BaseModel):
    """역 목록/검색 응답 항목 (역 선택 화면용).

    화면엔 역명만 노출되므로 station_idx(선택 식별자) + station_name만 반환한다.
    nat_code(열차정보 API 호출용 내부 코드)는 노출하지 않는다.
    """

    model_config = ConfigDict(from_attributes=True)

    station_idx: int = Field(..., description="역 PK (선택 시 식별자)")
    station_name: str = Field(..., description="역명")
