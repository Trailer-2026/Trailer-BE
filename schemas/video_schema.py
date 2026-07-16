# -*- coding: utf-8 -*-
from pydantic import BaseModel, Field


class BgmTrackResponse(BaseModel):
    """영상 빌더 BGM 선택 목록 항목."""

    file: str = Field(..., description="bgm 폴더 내 파일명 (렌더 요청의 bgm 값으로 사용)")
    title: str = Field(..., description="표시용 곡명")
    artist: str = Field(..., description="아티스트명 (파싱 실패 시 빈 문자열)")
    source: str = Field(..., description="음원 출처 (예: Pixabay, 없으면 빈 문자열)")


class VideoRenderStatusResponse(BaseModel):
    """여행 경로 3D 영상 렌더 작업 상태 (시작 응답·진행률 폴링 공용).

    POST /render 가 이 형태로 job_id 를 반환하고, GET /render/{job_id} 를
    폴링하면 percent/eta 가 갱신되다가 status=done 에서 video_url 이 채워진다.
    """

    job_id: str = Field(..., description="렌더 작업 ID (진행률 조회에 사용)")
    status: str = Field(..., description="작업 상태: running | done | failed")
    phase: str = Field(..., description="현재 단계 (렌더 준비 중 / 프레임 렌더링 / 후처리 / 완료)")
    percent: float = Field(..., description="진행률 0~100")
    frame: int = Field(..., description="렌더링된 프레임 번호 (프레임 단계에서만 증가)")
    total_frames: int | None = Field(None, description="전체 프레임 수 (프레임 단계 진입 전엔 null)")
    elapsed_seconds: float = Field(..., description="경과 시간(초)")
    eta_seconds: float | None = Field(None, description="예상 남은 시간(초) — 초반·완료 후엔 null 또는 0")
    engine: str = Field(..., description="렌더 엔진 (local | modal)")
    theme: str = Field(..., description="지도 계절 테마")
    light_preset: str | None = Field(None, description="시간대 조명 프리셋 (기본이면 null)")
    intro: bool = Field(..., description="TRAILER 인트로 포함 여부")
    outro: bool = Field(..., description="TRAILER 아웃트로 포함 여부")
    bgm: str | None = Field(None, description="BGM 파일명 (없으면 null)")
    video_url: str | None = Field(None, description="완성 영상 경로 (status=done 일 때만)")
    error: str | None = Field(None, description="실패 사유 (status=failed 일 때만)")
    log_tail: str = Field("", description="렌더 로그 끝부분 (종료 후 디버깅용)")
