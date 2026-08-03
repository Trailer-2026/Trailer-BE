# -*- coding: utf-8 -*-
from pydantic import BaseModel, Field


class BgmTrackResponse(BaseModel):
    """렌더 요청에 쓸 수 있는 BGM 트랙 목록 항목."""

    file: str = Field(..., description="bgm 폴더 내 파일명 (렌더 요청의 bgm 값으로 사용)")
    title: str = Field(..., description="표시용 곡명")
    artist: str = Field(..., description="아티스트명 (파싱 실패 시 빈 문자열)")
    source: str = Field(..., description="음원 출처 (예: Pixabay, 없으면 빈 문자열)")


class ReelsRecommendResponse(BaseModel):
    """릴스 무작위 추천 목록 항목."""

    reels_idx: int = Field(..., description="릴스 PK (재요청 시 exclude에 누적해 전달)")
    url: str = Field(..., description="릴스 영상 URL")
    title: str | None = Field(None, description="릴스 제목 (없으면 null)")
    like_count: int = Field(0, description="릴스 좋아요 수")
    comment_count: int = Field(0, description="릴스 댓글 수 (답글 포함)")
    is_liked: bool = Field(False, description="내가 좋아요한 릴스인지 (비로그인은 항상 false)")
    nickname: str | None = Field(None, description="릴스 작성자 닉네임 (작성자 없는 옛 릴스는 null)")
    profile_image: str | None = Field(None, description="릴스 작성자 프로필 사진 URL (작성자 없는 옛 릴스는 null)")


class ReelsShareResponse(BaseModel):
    """릴스 공유 링크."""

    share_url: str = Field(..., description="공유용 페이지 URL (공유 시트에 그대로 넘기면 됨)")
    title: str | None = Field(None, description="릴스 제목 (공유 문구용, 없으면 null)")


class VideoEditResponse(BaseModel):
    """릴스 영상 편집(구간 삭제 / 이미지 삽입) 결과."""

    reels_idx: int = Field(..., description="편집한 릴스 PK (PK 는 그대로, 영상만 교체됨)")
    video_url: str = Field(..., description="편집된 새 영상의 GCS 공개 URL — 릴스의 url 이 이 값으로 갱신됨")
    duration_seconds: float = Field(..., description="편집 후 영상 길이(초)")
    elapsed_seconds: float = Field(..., description="편집 처리 시간(초)")


class VideoRenderStatusResponse(BaseModel):
    """여행 경로 3D 영상 렌더 작업 상태 (시작 응답·진행률 폴링 공용).

    렌더 시작 API(POST /render/photos-only·photos-ordered·travel)가 이 형태로
    reels_idx 를 반환하고, GET /render/{reels_idx} 를 폴링하면 percent/eta 가
    갱신되다가 status=done 에서 video_url 이 채워진다. 릴스 행은 렌더 시작 시점에
    미리 만들어지므로(그 전엔 영상이 없어 추천 피드에 뜨지 않는다) 진행률 조회·
    다운로드·편집이 모두 같은 reels_idx 로 돈다.
    """

    reels_idx: int = Field(..., description="릴스 PK — 진행률 조회·다운로드·편집 공용 키")
    status: str = Field(
        ...,
        description="작업 상태: running | done | failed | unknown"
                    "(unknown = 서버 재시작으로 진행 정보가 사라진 미완료 릴스)",
    )
    phase: str = Field(..., description="현재 단계 (렌더 준비 중 / 프레임 렌더링 / 후처리 / 완료)")
    percent: float = Field(..., description="진행률 0~100")
    frame: int = Field(..., description="렌더링된 프레임 번호 (프레임 단계에서만 증가)")
    total_frames: int | None = Field(None, description="전체 프레임 수 (프레임 단계 진입 전엔 null)")
    elapsed_seconds: float = Field(..., description="경과 시간(초)")
    eta_seconds: float | None = Field(None, description="예상 남은 시간(초) — 초반·완료 후엔 null 또는 0")
    engine: str = Field(..., description='렌더 엔진 — 항상 "modal" (Modal T4 클라우드 전용)')
    theme: str = Field(..., description="지도 계절 테마 (진행 정보가 없는 unknown 상태면 빈 문자열)")
    bgm: str | None = Field(None, description="BGM 파일명 (없으면 null)")
    video_url: str | None = Field(
        None,
        description="완성 영상 URL (status=done 일 때만) — reels_url 과 같은 GCS 공개 URL",
    )
    reels_url: str | None = Field(None, description="GCS 버킷 공개 영상 URL (렌더 완료 시)")
    error: str | None = Field(None, description="실패 사유 (status=failed/unknown 일 때만)")
    log_tail: str = Field("", description="렌더 로그 끝부분 (종료 후 디버깅용)")
