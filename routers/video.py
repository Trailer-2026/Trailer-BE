# -*- coding: utf-8 -*-
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from core.response import CommonResponse
from core.security import get_current_user, get_optional_user
from databases.database import get_db
from databases.models.user import User
from schemas.video_schema import (
    BgmTrackResponse,
    ReelsRecommendResponse,
    ReelsShareResponse,
    VideoEditResponse,
    VideoRenderStatusResponse,
)
from services import video_service

router = APIRouter(prefix="/api/videos", tags=["Video"])

# Swagger 표기용 실제 BGM 파일명 — 서버 시작 시 bgm/ 폴더에서 읽는다.
# enum 을 스키마에만 얹으면 Swagger UI 는 드롭다운으로 보여주되(빈 값 = 무음),
# 서버 검증은 느슨한 매칭(곡명만 보내도 인식) 그대로 유지된다.
_BGM_NAMES = [track["file"] for track in video_service.list_bgm()]
_BGM_CHOICES = ["", *_BGM_NAMES]
_BGM_FILES = ", ".join(_BGM_NAMES) or "(없음)"


# 렌더 시작 엔드포인트들이 공유하는 폼 필드 — 문구·enum 을 한 곳에서만 관리한다.
def _bgm_form():
    return Form(
        "",
        description="BGM 파일명 또는 곡명 (빈 값이면 무음, 곡명만 보내도 매칭, 없으면 404). "
                    f"사용 가능: {_BGM_FILES}",
        json_schema_extra={"enum": _BGM_CHOICES},
    )


def _theme_form():
    return Form("default", description="지도 계절 테마: default|spring|summer|autumn|winter")


def _title_form(extra: str = ""):
    return Form(
        "",
        max_length=100,
        description=f"릴스 제목 (100자 이내, 빈 값이면 제목 없음{extra})",
    )


def _photo_payloads(photos: list[UploadFile]) -> list[tuple[str, bytes]]:
    return [(upload.filename or "", upload.file.read()) for upload in photos]


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
    "/reels/{reels_idx}/download",
    summary="내 완성 영상 다운로드 (reels_idx)",
    description="로그인 사용자 본인이 만든 릴스(reels_idx)의 완성 영상을 mp4 파일로 "
                "내려받습니다. 영상이 어디에 저장돼 있든(GCS 버킷 / 업로드 실패로 로컬에만 "
                "남은 경우) 서버가 알아서 찾아 reels_{reels_idx}.mp4 이름의 첨부 파일로 "
                "응답합니다. 편집으로 영상이 교체된 릴스도 항상 최신 영상이 내려갑니다. "
                "본인 릴스가 아니거나 릴스·영상 파일이 없으면 404(남의 릴스는 존재 여부를 "
                "알리지 않으려고 403이 아닌 404), 아직 렌더가 끝나지 않은 릴스면 400, "
                "저장소 접근에 실패하면 502를 반환합니다. "
                "JWT 인증이 필요하며 토큰이 없거나 유효하지 않으면 401을 반환합니다.",
    response_class=FileResponse,
)
def download_reels_video(
    reels_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    source, filename, size = video_service.get_reels_download(
        db, reels_idx, current_user.user_idx
    )
    if isinstance(source, Path):
        return FileResponse(source, media_type="video/mp4", filename=filename)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if size:
        headers["Content-Length"] = str(size)
    return StreamingResponse(source, media_type="video/mp4", headers=headers)


@router.post(
    "/render/photos-only",
    summary="사진만으로 영상 렌더링 시작 (여행 ID 없이)",
    description="여행 ID나 좌표 입력 없이, 업로드한 사진들의 EXIF 메타데이터에서 GPS 좌표와 "
                "촬영 시각을 추출해 촬영 시각 순서대로 지점을 이동하며 각 지점에서 해당 사진을 "
                "보여주는 영상 렌더링을 시작하고 reels_idx 를 즉시 반환합니다(진행률은 "
                "GET /api/videos/render/{reels_idx} 로 폴링). 릴스 행은 렌더 시작 시점에 "
                "로그인 사용자(user_idx)와 연결해 미리 만들어지고(영상이 없는 동안은 추천 "
                "피드에 뜨지 않음), 렌더가 끝나면 그 행의 url 에 GCS 영상 주소가 채워집니다. "
                "렌더에 실패하면 그 릴스 행은 삭제됩니다. title 을 주면 그 값이 릴스 "
                "제목이 되고, 비우면 제목 없는(null) 릴스가 됩니다. "
                "GPS 정보가 있는 사진이 2장 이상 필요하며(메신저 전송본은 GPS가 제거됨), "
                "GPS 없는 사진은 자동 제외됩니다. 지점 기준 1km 미만인 연속 사진들은 카메라 "
                "이동 없이 첫 사진 위치에 고정해 순서대로 보여주고, 1km 이상 떨어진 사진이 "
                "나오면 그 위치로 이동합니다. start_latitude/longitude 를 주면 그 위치(예: 서울역)를 출발지로 "
                "삼아 첫 사진 지점으로 이동하며 시작합니다. 조건을 못 채우면 400을 반환합니다. "
                "영상 앞뒤에는 TRAILER 인트로·아웃트로가 항상 붙습니다. JWT 인증이 필요하며 "
                "토큰이 없거나 유효하지 않으면 401을 반환합니다.",
    response_model=CommonResponse[VideoRenderStatusResponse],
)
def render_video_photos_only(
    start_name: str = Form("", description="출발지 라벨 (예: 서울역, 기본 '출발')"),
    start_latitude: float | None = Form(None, description="출발지 위도 (경도와 함께 지정, 생략 시 첫 사진 위치에서 시작)"),
    start_longitude: float | None = Form(None, description="출발지 경도 (위도와 함께 지정)"),
    bgm: str = _bgm_form(),
    theme: str = _theme_form(),
    title: str = _title_form(),
    photos: list[UploadFile] = File(..., description="여행 사진들 (EXIF GPS 필요, 최소 2장)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = video_service.start_render_photos_only(
        db,
        photos=_photo_payloads(photos),
        user_idx=current_user.user_idx,
        bgm=bgm,
        theme=theme,
        title=title,
        start_name=start_name,
        start_latitude=start_latitude,
        start_longitude=start_longitude,
    )
    return CommonResponse.success_response("영상 렌더링 시작", data=job)


@router.post(
    "/render/photos-ordered",
    summary="사진 순서 지정 영상 렌더링 시작 (업로드 순서대로)",
    description="POST /render/photos-only 와 같지만 촬영 시각을 무시하고, 업로드한 사진 "
                "순서 그대로(사용자가 지정한 순서) 지점을 이동하며 각 지점에서 해당 사진을 "
                "보여주는 영상 렌더링을 시작하고 reels_idx 를 즉시 반환합니다(진행률은 "
                "GET /api/videos/render/{reels_idx} 로 폴링). EXIF 에서는 GPS 좌표만 사용합니다. "
                "GPS 정보가 있는 사진이 2장 이상 필요하며(메신저 전송본은 GPS가 제거됨), "
                "GPS 없는 사진은 자동 제외되어 순서에서 빠집니다. 직전 지점 기준 1km 미만인 "
                "연속 사진들은 카메라 이동 없이 그 지점에 고정해 순서대로 보여주고, 1km 이상 "
                "떨어진 사진이 나오면 그 위치로 이동합니다. start_latitude/longitude 를 주면 "
                "그 위치(예: 서울역)를 출발지로 삼아 첫 사진 지점으로 이동하며 시작합니다. "
                "조건을 못 채우면 400을 반환합니다. 영상 앞뒤에는 TRAILER 인트로·아웃트로가 "
                "항상 붙습니다. 릴스 행은 렌더 시작 시점에 로그인 사용자(user_idx)와 연결해 "
                "미리 만들어지고, 렌더가 끝나면 그 행의 url 에 GCS 영상 주소가 채워집니다"
                "(실패 시 릴스 행 삭제). title 을 주면 그 값이 릴스 제목이 되고, 비우면 "
                "제목 없는(null) 릴스가 됩니다. "
                "JWT 인증이 필요하며 토큰이 없거나 유효하지 않으면 "
                "401을 반환합니다.",
    response_model=CommonResponse[VideoRenderStatusResponse],
)
def render_video_photos_ordered(
    start_name: str = Form("", description="출발지 라벨 (예: 서울역, 기본 '출발')"),
    start_latitude: float | None = Form(None, description="출발지 위도 (경도와 함께 지정, 생략 시 첫 사진 위치에서 시작)"),
    start_longitude: float | None = Form(None, description="출발지 경도 (위도와 함께 지정)"),
    bgm: str = _bgm_form(),
    theme: str = _theme_form(),
    title: str = _title_form(),
    photos: list[UploadFile] = File(..., description="여행 사진들 (EXIF GPS 필요, 최소 2장) — 보낸 순서가 곧 영상 순서"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = video_service.start_render_photos_only(
        db,
        photos=_photo_payloads(photos),
        user_idx=current_user.user_idx,
        bgm=bgm,
        theme=theme,
        title=title,
        start_name=start_name,
        start_latitude=start_latitude,
        start_longitude=start_longitude,
        sort_by_time=False,
    )
    return CommonResponse.success_response("영상 렌더링 시작", data=job)


@router.post(
    "/render/travel",
    summary="여행 일정으로 영상 렌더링 시작 (travel_idx)",
    description="저장된 여행(travel_idx)의 일정(schedule)을 타임라인 순(day_no, sequence)으로 "
                "따라가며 각 일정의 좌표로 이동 경로를 만들고, 일정에 첨부된 여행 "
                "이미지(travel_image)를 해당 지점에서 보여주는 영상 렌더링을 시작하고 "
                "reels_idx 를 즉시 반환합니다(진행률은 GET /api/videos/render/{reels_idx} 로 "
                "폴링). 직전 지점 "
                "기준 1km 미만인 연속 일정은 별도 지점 없이 한 지점으로 묶여 사진만 이어서 "
                "나오고, 기차 일정은 출발역 좌표가 경유 지점이 됩니다. 이미지 다운로드에 "
                "실패한 이미지는 건너뜁니다. 본인 여행이 아니거나 없으면 404, 여행에 일정이 "
                "없거나 지점이 2개 미만이면 400을 반환합니다. 영상 앞뒤에는 TRAILER "
                "인트로·아웃트로가 항상 붙습니다. 릴스 행은 렌더 시작 시점에 로그인 "
                "사용자(user_idx)와 연결해 미리 만들어지고, 렌더가 끝나면 그 행의 url 에 GCS "
                "영상 주소가 채워집니다(실패 시 릴스 행 삭제). title 을 주면 그 값이 릴스 "
                "제목이 되고, 비우면 그 여행의 제목(travel.title)이 릴스 제목으로 들어갑니다. "
                "JWT 인증이 필요하며 토큰이 "
                "없거나 유효하지 않으면 401을 반환합니다.",
    response_model=CommonResponse[VideoRenderStatusResponse],
)
def render_video_from_travel(
    travel_idx: int = Form(..., description="영상을 만들 여행 PK (본인 여행만 가능)"),
    bgm: str = _bgm_form(),
    theme: str = _theme_form(),
    title: str = _title_form(", 여행 제목이 대신 들어감"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = video_service.start_render_travel(
        db,
        current_user,
        travel_idx,
        bgm=bgm,
        theme=theme,
        title=title,
    )
    return CommonResponse.success_response("영상 렌더링 시작", data=job)


@router.get(
    "/reels/recommend",
    summary="릴스 무작위 추천",
    description="릴스를 무작위로 10개 추천합니다. 재요청 시 이미 받은 reels_idx들을 exclude에 "
                "쉼표로 구분해 넘기면 그 릴스들을 제외하고 새로 뽑으며, 남은 릴스가 10개 "
                "미만이면 있는 만큼만 반환합니다. 제외하고 남은 릴스가 하나도 없으면(전부 "
                "한 번씩 추천됨) exclude를 무시하고 처음부터 다시 추천합니다. 각 릴스에는 "
                "제목(title)과 좋아요 수(like_count)·댓글 수(comment_count, 답글 포함)가 함께 "
                "내려갑니다. 로그인 상태로 호출하면 내가 좋아요한 릴스는 is_liked 가 true 로 "
                "내려가고(비로그인은 항상 false), 또 "
                "작성자의 닉네임(nickname)·프로필 사진(profile_image)이 함께 내려가며, "
                "작성자 없는 옛 릴스는 둘 다 null 입니다. exclude 형식이 잘못되면 400을 "
                "반환합니다.\n\n"
                "**홈 화면의 '지금 사람들이 떠나는 여행' 카드도 이 API를 씁니다** — limit 으로 "
                "필요한 개수(예: 2)만 받으면 됩니다. 카드에 필요한 지역 태그(region)와 "
                "썸네일 이미지(thumbnail_url)가 함께 내려가며, 렌더 전에 만들어진 옛 릴스는 "
                "둘 다 null 이라 그 땐 지역 핀을 숨기고 url 영상의 첫 프레임을 카드 이미지로 "
                "쓰면 됩니다.\n\n"
                "인증은 선택입니다 — JWT를 보내면 내가 차단한 사용자의 릴스가 결과에서 빠지고, "
                "토큰이 없거나 만료됐으면 비로그인으로 간주해 전체에서 추천합니다(401 없음).",
    response_model=CommonResponse[list[ReelsRecommendResponse]],
)
def recommend_reels(
    exclude: str = Query("", description="제외할 reels_idx 목록 (쉼표 구분, 예: 1,5,9 — 이전 응답의 idx 누적)"),
    limit: int = Query(10, ge=1, le=10, description="받을 릴스 개수 (홈 화면 카드는 2)"),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
):
    reels = video_service.recommend_reels(db, exclude, current_user, limit)
    return CommonResponse.success_response("릴스 추천 목록 조회 성공", data=reels)


@router.get(
    "/reels/{reels_idx}/share",
    summary="릴스 공유 링크 조회 (reels_idx)",
    description="릴스(reels_idx)를 공유할 때 쓸 링크를 반환합니다. share_url 은 영상이 재생되는 "
                "공유 페이지 주소(/r/{reels_idx})로, 카톡·SNS 공유 시트에 그대로 넘기면 됩니다. "
                "버킷 영상 주소를 직접 공유할 때와 달리 링크 미리보기가 뜨고, 삭제되거나 아직 "
                "렌더 중인 릴스는 그 페이지가 404가 되어 공유 링크가 끊깁니다. 앱 안에서 바로 "
                "재생할 영상 주소는 릴스 목록의 url 을 그대로 쓰면 됩니다. "
                "본인 릴스가 아니어도 조회할 수 있습니다"
                "(릴스는 공개 피드). 없거나 삭제됐거나 아직 렌더가 끝나지 않은 릴스면 404를 "
                "반환합니다. 로그인 없이 호출할 수 있습니다.",
    response_model=CommonResponse[ReelsShareResponse],
)
def get_reels_share_link(
    reels_idx: int,
    request: Request,
    db: Session = Depends(get_db),
):
    data = video_service.get_share_link(db, reels_idx, str(request.base_url))
    return CommonResponse.success_response("공유 링크 조회 성공", data=data)


@router.post(
    "/edit/cut",
    summary="릴스 영상 구간 삭제 (reels_idx)",
    description="등록된 릴스(reels_idx)의 영상에서 [시작, 끝) 구간을 잘라냅니다. 편집본을 "
                "GCS 버킷에 새로 올린 뒤 릴스의 url 을 그 URL 로 교체하고(릴스 PK 는 그대로) "
                "편집 전 영상 객체는 버킷에서 삭제합니다. 아직 렌더가 끝나지 않은 릴스거나 "
                "구간이 잘못됐거나 영상 전체를 "
                "지우려 하면 400, 본인 릴스가 아니거나 없으면 404, ffmpeg 처리 실패 시 502를 "
                "반환합니다. JWT 인증이 필요하며 토큰이 없거나 유효하지 않으면 401을 "
                "반환합니다.",
    response_model=CommonResponse[VideoEditResponse],
)
def cut_video_section(
    reels_idx: int = Form(..., description="편집할 릴스 PK (본인 릴스만 가능)"),
    start_seconds: float = Form(..., description="삭제 구간 시작(초)"),
    end_seconds: float = Form(..., description="삭제 구간 끝(초)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = video_service.cut_video(
        db, reels_idx, current_user.user_idx, start_seconds, end_seconds
    )
    return CommonResponse.success_response("구간 삭제 성공", data=result)


@router.post(
    "/edit/insert",
    summary="릴스 영상 이미지 삽입 (reels_idx)",
    description="등록된 릴스(reels_idx)의 영상 at_seconds 시점에 업로드한 이미지를 전체 "
                "화면으로 끼워 넣습니다. 본편이 그 시점에서 멈추고 사진이 나온 뒤 이어서 "
                "재생되며, 사진이 머무는 시간은 영상 렌더링 때 사진 한 장을 보여주는 시간과 "
                "동일하게 고정입니다(지정 불가, 사진은 화면 꽉 채움 후 중앙 크롭). 편집본을 "
                "GCS 버킷에 새로 올린 뒤 릴스의 url 을 그 URL 로 교체하고(릴스 PK 는 그대로) "
                "편집 전 영상 객체는 버킷에서 삭제합니다. 아직 렌더가 끝나지 않은 릴스거나 "
                "시점·이미지가 잘못되면 400, 본인 "
                "릴스가 아니거나 없으면 404, ffmpeg 처리 실패 시 502를 반환합니다. JWT "
                "인증이 필요하며 토큰이 없거나 유효하지 않으면 401을 반환합니다.",
    response_model=CommonResponse[VideoEditResponse],
)
def insert_image_clip(
    reels_idx: int = Form(..., description="편집할 릴스 PK (본인 릴스만 가능)"),
    at_seconds: float = Form(..., description="이미지를 끼워 넣을 시점(초), 0 ~ 영상 길이"),
    image: UploadFile = File(..., description="삽입할 이미지 파일 (jpg/png/webp 등)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = video_service.insert_image(
        db, reels_idx, current_user.user_idx, at_seconds,
        image.filename or "", image.file.read(),
    )
    return CommonResponse.success_response("이미지 삽입 성공", data=result)


@router.get(
    "/render/{reels_idx}",
    summary="영상 렌더링 진행률 조회 (reels_idx)",
    description="렌더 시작 응답으로 받은 reels_idx 로 진행률(percent), 현재 단계, 경과/예상 "
                "남은 시간을 조회합니다. status 가 done 이면 video_url(=reels_url)로 영상을 "
                "받을 수 있고, failed 면 error 에 사유가 담기며 그 릴스 행은 삭제됩니다. "
                "진행 정보는 서버 메모리에만 유지되므로 렌더 도중 서버가 재시작되면 "
                "status=unknown 으로 응답합니다(완료된 릴스는 재시작 후에도 done). 본인 "
                "릴스가 아니거나 없으면 404. JWT 인증이 필요하며 토큰이 없거나 유효하지 "
                "않으면 401을 반환합니다.",
    response_model=CommonResponse[VideoRenderStatusResponse],
)
def get_render_status(
    reels_idx: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    status = video_service.get_render_job(db, reels_idx, current_user.user_idx)
    return CommonResponse.success_response("렌더링 상태 조회 성공", data=status)
