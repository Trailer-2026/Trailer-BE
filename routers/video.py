# -*- coding: utf-8 -*-
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from core.response import CommonResponse
from databases.database import get_db
from schemas.video_schema import (
    ReelsRecommendResponse,
    VideoEditResponse,
    VideoRenderStatusResponse,
)
from services import video_service

router = APIRouter(prefix="/api/videos", tags=["Video"])


@router.get("/assets/map_themes.js", include_in_schema=False)
def get_map_themes() -> FileResponse:
    # 빌더 미리보기가 렌더러와 같은 테마 모듈을 공유한다. 브라우저가 이전 버전을
    # 캐시하면 테마가 안 바뀌는 것처럼 보이므로 매번 재검증하게 한다.
    return FileResponse(
        video_service.get_map_themes_path(),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.get(
    "/output/{name}",
    summary="완성 영상 다운로드",
    description="렌더링이 끝난 mp4 파일을 반환합니다. 렌더 응답의 video_url 이 이 경로를 "
                "가리킵니다. 존재하지 않으면 404를 반환합니다.",
    response_class=FileResponse,
)
def get_output_video(name: str) -> FileResponse:
    return FileResponse(video_service.get_output_path(name), media_type="video/mp4", filename=name)


@router.post(
    "/render",
    summary="좌표 직접 입력으로 영상 렌더링 시작",
    description="GPS 지점 목록(points)과 지점별 사진, BGM/테마/조명/인트로·아웃트로 옵션을 "
                "받아 세로형(1080x1920) 3D 지도 여행 영상 렌더링을 시작하고 job_id 를 즉시 "
                "반환합니다(multipart/form-data). 진행률·완료 여부는 "
                "GET /api/videos/render/{job_id} 로 폴링하세요. engine=local 은 서버 GPU, "
                "engine=modal 은 Modal T4 클라우드에서 렌더링합니다.",
    response_model=CommonResponse[VideoRenderStatusResponse],
)
def render_video(
    points: str = Form(..., description='GPS 지점 JSON 배열 문자열: [{"latitude","longitude","name"}, ...] (최소 2개)'),
    bgm: str = Form("", description="BGM 파일명 (GET /api/videos/bgm 의 file 값, 빈 값이면 무음)"),
    quick: str = Form("false", description='"true"면 저해상도 빠른 렌더 (local: 540x960/15fps, modal: JPEG q95)'),
    engine: str = Form("local", description="렌더 엔진: local(서버 GPU) | modal(Modal T4 클라우드)"),
    theme: str = Form("default", description="지도 계절 테마: default|spring|summer|autumn|winter"),
    light_preset: str = Form("", description="시간대 조명: dawn|day|dusk|night (빈 값 = 테마 기본)"),
    intro: str = Form("false", description='"true"면 TRAILER 인트로 클립을 앞에 붙임'),
    outro: str = Form("false", description='"true"면 TRAILER 아웃트로 클립을 뒤에 붙임'),
    photo_points: str = Form("[]", description="photos 각 파일이 속한 지점 인덱스 JSON 배열 (photos와 개수 일치)"),
    # fastapi 0.110 에서는 `list[UploadFile] | None = None` 이 422 를 내므로
    # File([]) 기본값으로 선언해야 파일 없이도 빈 리스트로 들어온다.
    photos: list[UploadFile] = File([], description="지점별 첨부 사진 파일들 (없으면 생략)"),
):
    photo_payloads = [
        (upload.filename or "", upload.file.read()) for upload in (photos or [])
    ]
    job = video_service.start_render(
        points_json=points,
        photo_points_json=photo_points,
        photos=photo_payloads,
        bgm=bgm,
        quick=quick.lower().strip() == "true",
        engine=engine,
        theme=theme,
        light_preset=light_preset,
        intro=intro.lower().strip() == "true",
        outro=outro.lower().strip() == "true",
    )
    return CommonResponse.success_response("영상 렌더링 시작", data=job)


@router.post(
    "/render/photos-only",
    summary="사진만으로 영상 렌더링 시작 (여행 ID 없이)",
    description="여행 ID나 좌표 입력 없이, 업로드한 사진들의 EXIF 메타데이터에서 GPS 좌표와 "
                "촬영 시각을 추출해 촬영 시각 순서대로 지점을 이동하며 각 지점에서 해당 사진을 "
                "보여주는 영상 렌더링을 시작하고 job_id 를 즉시 반환합니다(진행률 폴링은 "
                "POST /render 와 동일). "
                "GPS 정보가 있는 사진이 2장 이상 필요하며(메신저 전송본은 GPS가 제거됨), "
                "GPS 없는 사진은 자동 제외됩니다. 지점 기준 1km 미만인 연속 사진들은 카메라 "
                "이동 없이 첫 사진 위치에 고정해 순서대로 보여주고, 1km 이상 떨어진 사진이 "
                "나오면 그 위치로 이동합니다. start_latitude/longitude 를 주면 그 위치(예: 서울역)를 출발지로 "
                "삼아 첫 사진 지점으로 이동하며 시작합니다. 조건을 못 채우면 400을 반환합니다. "
                "렌더가 끝나면 완성 영상이 GCS 버킷에 올라가고 reels 테이블에 여행/사용자 "
                "연결 없이 자동 등록되며, 상태 응답의 reels_idx/reels_url 로 확인할 수 "
                "있습니다.",
    response_model=CommonResponse[VideoRenderStatusResponse],
)
def render_video_photos_only(
    start_name: str = Form("", description="출발지 라벨 (예: 서울역, 기본 '출발')"),
    start_latitude: float | None = Form(None, description="출발지 위도 (경도와 함께 지정, 생략 시 첫 사진 위치에서 시작)"),
    start_longitude: float | None = Form(None, description="출발지 경도 (위도와 함께 지정)"),
    bgm: str = Form("", description="BGM 파일명 (GET /api/videos/bgm 의 file 값, 빈 값이면 무음)"),
    quick: str = Form("false", description='"true"면 저해상도 빠른 렌더'),
    engine: str = Form("local", description="렌더 엔진: local(서버 GPU) | modal(Modal T4 클라우드)"),
    theme: str = Form("default", description="지도 계절 테마: default|spring|summer|autumn|winter"),
    light_preset: str = Form("", description="시간대 조명: dawn|day|dusk|night (빈 값 = 테마 기본)"),
    intro: str = Form("false", description='"true"면 TRAILER 인트로 클립을 앞에 붙임'),
    outro: str = Form("false", description='"true"면 TRAILER 아웃트로 클립을 뒤에 붙임'),
    photos: list[UploadFile] = File(..., description="여행 사진들 (EXIF GPS 필요, 최소 2장)"),
):
    photo_payloads = [
        (upload.filename or "", upload.file.read()) for upload in photos
    ]
    job = video_service.start_render_photos_only(
        photos=photo_payloads,
        bgm=bgm,
        quick=quick.lower().strip() == "true",
        engine=engine,
        theme=theme,
        light_preset=light_preset,
        intro=intro.lower().strip() == "true",
        outro=outro.lower().strip() == "true",
        start_name=start_name,
        start_latitude=start_latitude,
        start_longitude=start_longitude,
    )
    return CommonResponse.success_response("영상 렌더링 시작", data=job)


@router.get(
    "/reels/recommend",
    summary="릴스 무작위 추천",
    description="릴스를 무작위로 10개 추천합니다. 재요청 시 이미 받은 reels_idx들을 exclude에 "
                "쉼표로 구분해 넘기면 그 릴스들을 제외하고 새로 뽑으며, 남은 릴스가 10개 "
                "미만이면 있는 만큼만 반환합니다. 제외하고 남은 릴스가 하나도 없으면(전부 "
                "한 번씩 추천됨) exclude를 무시하고 처음부터 다시 추천합니다. exclude 형식이 "
                "잘못되면 400을 반환합니다.",
    response_model=CommonResponse[list[ReelsRecommendResponse]],
)
def recommend_reels(
    exclude: str = Query("", description="제외할 reels_idx 목록 (쉼표 구분, 예: 1,5,9 — 이전 응답의 idx 누적)"),
    db: Session = Depends(get_db),
):
    reels = video_service.recommend_reels(db, exclude)
    return CommonResponse.success_response("릴스 추천 목록 조회 성공", data=reels)


@router.post(
    "/edit/cut",
    summary="영상 구간 삭제",
    description="완성 영상에서 [시작, 끝) 구간을 잘라낸 새 영상을 만듭니다. 원본은 보존되며 "
                "편집본은 새 파일명으로 저장됩니다. 구간이 잘못됐거나 영상 전체를 지우려 하면 "
                "400, 영상이 없으면 404, ffmpeg 처리 실패 시 502를 반환합니다.",
    response_model=CommonResponse[VideoEditResponse],
)
def cut_video_section(
    name: str = Form(..., description="편집할 완성 영상 파일명 (video_url 마지막 경로 요소)"),
    start_seconds: float = Form(..., description="삭제 구간 시작(초)"),
    end_seconds: float = Form(..., description="삭제 구간 끝(초)"),
):
    result = video_service.cut_video(name, start_seconds, end_seconds)
    return CommonResponse.success_response("구간 삭제 성공", data=result)


@router.post(
    "/edit/insert",
    summary="영상 이미지 삽입",
    description="완성 영상의 at_seconds 시점에 업로드한 이미지를 전체 화면으로 끼워 넣은 새 "
                "영상을 만듭니다. 본편이 그 시점에서 멈추고 사진이 photo_seconds 초 나온 뒤 "
                "이어서 재생되므로 영상 길이가 그만큼 늘어납니다(사진은 화면 꽉 채움 후 중앙 "
                "크롭). 원본은 보존됩니다. 시점·이미지가 잘못되면 400, 영상이 없으면 404, "
                "ffmpeg 처리 실패 시 502를 반환합니다.",
    response_model=CommonResponse[VideoEditResponse],
)
def insert_image_clip(
    name: str = Form(..., description="편집할 완성 영상 파일명 (video_url 마지막 경로 요소)"),
    at_seconds: float = Form(..., description="이미지를 끼워 넣을 시점(초), 0 ~ 영상 길이"),
    photo_seconds: float = Form(1.5, description="사진 표시 시간(초), 0.3 ~ 10 (기본 1.5)"),
    image: UploadFile = File(..., description="삽입할 이미지 파일 (jpg/png/webp 등)"),
):
    result = video_service.insert_image(
        name, at_seconds, image.filename or "", image.file.read(), photo_seconds
    )
    return CommonResponse.success_response("이미지 삽입 성공", data=result)


@router.get(
    "/render/{job_id}",
    summary="영상 렌더링 진행률 조회",
    description="렌더 작업의 진행률(percent), 현재 단계, 경과/예상 남은 시간을 반환합니다. "
                "status 가 done 이면 video_url 로 영상을 받을 수 있고, failed 면 error 에 "
                "사유가 담깁니다. 존재하지 않는 job_id 면 404를 반환합니다. "
                "작업 목록은 서버 메모리에만 유지되므로 서버 재시작 시 사라집니다.",
    response_model=CommonResponse[VideoRenderStatusResponse],
)
def get_render_status(job_id: str):
    status = video_service.get_render_job(job_id)
    return CommonResponse.success_response("렌더링 상태 조회 성공", data=status)
