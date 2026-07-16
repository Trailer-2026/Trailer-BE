# -*- coding: utf-8 -*-
from pydantic import BaseModel, Field


class BgmTrackResponse(BaseModel):
    """영상 빌더 BGM 선택 목록 항목."""

    file: str = Field(..., description="bgm 폴더 내 파일명 (렌더 요청의 bgm 값으로 사용)")
    title: str = Field(..., description="표시용 곡명")
    artist: str = Field(..., description="아티스트명 (파싱 실패 시 빈 문자열)")
    source: str = Field(..., description="음원 출처 (예: Pixabay, 없으면 빈 문자열)")


class VideoRenderResponse(BaseModel):
    """여행 경로 3D 영상 렌더링 결과."""

    video_url: str = Field(..., description="완성 영상 경로 (GET /api/videos/output/{name})")
    engine: str = Field(..., description="사용한 렌더 엔진 (local | modal)")
    theme: str = Field(..., description="적용한 지도 계절 테마")
    light_preset: str | None = Field(None, description="적용한 시간대 조명 프리셋 (기본이면 null)")
    intro: bool = Field(..., description="TRAILER 인트로 클립 포함 여부")
    outro: bool = Field(..., description="TRAILER 아웃트로 클립 포함 여부")
    bgm: str | None = Field(None, description="사용한 BGM 파일명 (없으면 null)")
    elapsed_seconds: float = Field(..., description="렌더링 소요 시간(초)")
    log_tail: str = Field(..., description="렌더 로그 끝부분 (디버깅용)")
