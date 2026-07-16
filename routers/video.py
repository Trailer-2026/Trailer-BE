# -*- coding: utf-8 -*-
from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from core.response import CommonResponse
from schemas.video_schema import (
    BgmTrackResponse,
    VideoEditResponse,
    VideoRenderStatusResponse,
)
from services import video_service

router = APIRouter(prefix="/api/videos", tags=["Video"])


@router.get(
    "/builder",
    summary="영상 빌더 페이지",
    description="여행 경로 3D 영상 빌더 HTML 페이지를 반환합니다. 지도를 클릭해 GPS 지점을 "
                "추가하고 사진/BGM/테마를 골라 POST /api/videos/render 로 렌더링을 요청하는 "
                "개발용 UI입니다. Mapbox 토큰은 서버가 주입합니다.",
    response_class=HTMLResponse,
)
def get_builder_page() -> HTMLResponse:
    return HTMLResponse(video_service.get_builder_html())


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
    "/bgm",
    summary="BGM 목록 조회",
    description="영상에 입힐 수 있는 BGM 트랙 목록을 반환합니다. file 값을 렌더 요청의 "
                "bgm 필드에 그대로 넣으면 됩니다.",
    response_model=CommonResponse[list[BgmTrackResponse]],
)
def get_bgm_list():
    tracks = video_service.list_bgm()
    return CommonResponse.success_response("BGM 목록 조회 성공", data=tracks)


@router.get(
    "/bgm/preview",
    summary="BGM 미리듣기",
    description="BGM 파일을 오디오 스트림으로 반환합니다. 존재하지 않는 파일이면 404, "
                "오디오가 아니면 400을 반환합니다.",
    response_class=FileResponse,
)
def get_bgm_preview(file: str = Query(..., description="BGM 파일명 (GET /api/videos/bgm 의 file 값)")):
    return FileResponse(video_service.get_bgm_path(file), media_type="audio/mpeg")


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
    summary="여행 경로 영상 렌더링 시작",
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
    "/edit/overlay",
    summary="영상 이미지 삽입",
    description="완성 영상의 [시작, 끝) 구간에 업로드한 이미지를 화면 중앙에 오버레이한 새 "
                "영상을 만듭니다(비율 유지, 프레임의 86% 박스에 맞춤). 원본은 보존됩니다. "
                "구간·이미지가 잘못되면 400, 영상이 없으면 404, ffmpeg 처리 실패 시 502를 "
                "반환합니다.",
    response_model=CommonResponse[VideoEditResponse],
)
def insert_image_overlay(
    name: str = Form(..., description="편집할 완성 영상 파일명 (video_url 마지막 경로 요소)"),
    start_seconds: float = Form(..., description="이미지 표시 시작(초)"),
    end_seconds: float = Form(..., description="이미지 표시 끝(초)"),
    image: UploadFile = File(..., description="삽입할 이미지 파일 (jpg/png/webp 등)"),
):
    result = video_service.overlay_image(
        name, start_seconds, end_seconds, image.filename or "", image.file.read()
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
