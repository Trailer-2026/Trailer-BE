"""GCS(Cloud Storage) 업로드 유틸.

버킷 이름은 [gcs] bucket_name, 인증은 [firebase] credentials(같은 GCP 프로젝트
trailer-7ef0a 의 서비스 계정 키 — Storage 객체 관리자 권한 부여됨)를 재사용한다.
버킷은 공개 읽기(allUsers 뷰어)라 업로드 후 반환하는 URL을 그대로 프론트에 내려준다.
"""
import io
import json
import logging
import uuid
from functools import lru_cache

from PIL import Image
from google.cloud import storage
from google.oauth2 import service_account

from config import Config, parser
from core.exceptions.custom import BadRequestException, ExternalServiceException

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 업로드 이미지 상한 — 프로필 사진·여행 대표 사진 공용

# 허용 포맷(Pillow가 판별한 이름 → 저장할 MIME). 여기 없는 건 전부 거절한다 — 특히 **SVG는
# 안 받는다**: 버킷이 공개 읽기라 <script>가 든 SVG를 image/svg+xml로 서비스하면 그 URL을 직접
# 여는 순간 스크립트가 돈다.
_ALLOWED_IMAGE_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}
# HEIC/HEIF(아이폰 원본)는 pillow-heif 없이는 Pillow가 못 읽어 매직바이트로 확인한다
# (ISO-BMFF: 4~8바이트가 'ftyp', 그 다음이 브랜드). 디코더 추가 없이 기존 업로드를 안 깨려는 것.
_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1")


@lru_cache(maxsize=1)
def _bucket() -> storage.Bucket:
    bucket_name = Config.read("gcs", "bucket_name")
    # 크리덴셜 JSON 안의 %40 등이 configparser 보간에 걸리므로 raw로 읽는다
    raw = parser.get("firebase", "credentials", raw=True, fallback=None)
    if not bucket_name or not raw:
        raise ExternalServiceException(
            "GCS 설정이 없습니다. properties_dev.ini 의 [gcs] bucket_name / [firebase] credentials 를 확인하세요."
        )
    info = json.loads(raw)
    credentials = service_account.Credentials.from_service_account_info(info)
    client = storage.Client(project=info["project_id"], credentials=credentials)
    return client.bucket(bucket_name)


def _public_prefix() -> str:
    """이 버킷 공개 URL 의 고정 접두어 (업로드 반환 URL·역변환이 같은 형식을 공유)."""
    return f"https://storage.googleapis.com/{_bucket().name}/"


def upload_bytes(object_path: str, data: bytes, content_type: str) -> str:
    """바이트를 버킷의 object_path 에 올리고 공개 URL을 반환한다."""
    try:
        _bucket().blob(object_path).upload_from_string(data, content_type=content_type)
    except ExternalServiceException:
        raise
    except Exception as exc:
        logger.exception("GCS 업로드 실패: %s", object_path)
        raise ExternalServiceException("영상 저장소 업로드에 실패했습니다.") from exc
    return _public_prefix() + object_path


def upload_file(object_path: str, path, content_type: str) -> str:
    """로컬 파일을 버킷의 object_path 에 스트리밍 업로드하고 공개 URL을 반환한다.

    큰 영상 파일을 통째로 메모리에 올리지 않도록 upload_bytes 대신 사용한다.
    """
    try:
        _bucket().blob(object_path).upload_from_filename(str(path), content_type=content_type)
    except ExternalServiceException:
        raise
    except Exception as exc:
        logger.exception("GCS 업로드 실패: %s", object_path)
        raise ExternalServiceException("영상 저장소 업로드에 실패했습니다.") from exc
    return _public_prefix() + object_path


def upload_image(
    path_prefix: str, data: bytes, content_type: str | None, filename: str | None
) -> str:
    """업로드된 이미지 1장을 검증해 `<path_prefix>/<uuid><ext>`로 올리고 공개 URL을 반환한다.

    프로필 사진·여행 대표 사진이 같은 검증(형식·빈 파일·상한)을 공유한다 — 한 곳만 고친다.
    저장 Content-Type은 바이트에서 판별한 값이라 클라이언트가 뭘 적어 보내든 실물과 일치한다.
    확장자는 파일명에서 살린다(브라우저 표시·다운로드 이름용, 없으면 확장자 없이 올린다).

    확장자는 클라이언트가 준 파일명에서 오므로 영숫자 5자 이내만 받는다 — 'a.jpg/../x' 같은
    파일명이 객체 경로 모양을 바꾸지 못하게 한다(GCS는 슬래시를 경로로 해석한다).
    """
    if not content_type or not content_type.startswith("image/"):
        raise BadRequestException("이미지 파일만 업로드할 수 있습니다.")
    if not data:
        raise BadRequestException("빈 파일입니다.")
    if len(data) > MAX_IMAGE_BYTES:
        raise BadRequestException("이미지는 10MB 이하만 가능합니다.")

    # 저장할 MIME은 클라이언트가 말한 content_type이 아니라 **바이트에서 판별한 것**을 쓴다.
    # 공개 버킷이라 응답 Content-Type이 곧 브라우저 해석 방식이 된다.
    mime = _detect_image_mime(data)
    if mime is None:
        raise BadRequestException("지원하지 않는 이미지 형식입니다(jpg·png·webp·gif·heic).")

    raw_ext = filename.rsplit(".", 1)[-1].lower() if filename and "." in filename else ""
    ext = f".{raw_ext}" if raw_ext.isalnum() and len(raw_ext) <= 5 else ""
    return upload_bytes(f"{path_prefix}/{uuid.uuid4().hex}{ext}", data, mime)


def _detect_image_mime(data: bytes) -> str | None:
    """이미지 바이트 → 저장할 MIME. 허용 포맷이 아니면 None.

    Pillow가 헤더를 파싱해 **실제 포맷**을 읽는다 — 선언된 content_type이나 확장자와 달리 위조가
    안 되므로, SVG·HTML을 image/png라고 적어 보내도 여기서 걸린다. 픽셀 폭탄도 open 단계에서
    DecompressionBombError로 막힌다(우리가 디코딩하기 전에 크기부터 본다).

    `verify()`는 PNG만 실제 구현이 있고(청크 CRC 검사) JPEG·GIF·WEBP는 no-op이다. 그래서
    ponytail: **헤더가 온전한 채 뒤가 잘린 JPEG/GIF는 통과해 저장된다** — 픽셀을 다 펼쳐 봐야
    잡히는데(8000x6000 = RGB 144MB) 업로드마다 그 비용을 낼 값이 없다. 결과는 깨진 썸네일 한
    장이고 사용자가 다시 올리면 된다. 정말 걸러야 하면 img.load() + MAX_IMAGE_PIXELS 축소.

    HEIC만 디코더가 없어(pillow-heif 미설치) 매직바이트로 본다.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format or ""
            img.verify()  # PNG면 청크 CRC까지 검사, 나머지 포맷은 no-op(위 docstring 참고)
    except Exception:  # noqa: BLE001  Pillow는 포맷마다 다른 예외를 던진다
        if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
            return "image/heic"
        return None
    return _ALLOWED_IMAGE_MIME.get(fmt)


def object_path_from_url(url: str) -> str | None:
    """이 버킷의 공개 URL 이면 객체 경로를, 아니면 None 을 반환한다."""
    prefix = _public_prefix()
    return url[len(prefix):] if url.startswith(prefix) else None


def download_bytes(object_path: str) -> bytes:
    """버킷의 object_path 객체를 바이트로 내려받는다."""
    try:
        return _bucket().blob(object_path).download_as_bytes()
    except ExternalServiceException:
        raise
    except Exception as exc:
        logger.exception("GCS 다운로드 실패: %s", object_path)
        raise ExternalServiceException("영상 저장소에서 이미지를 받지 못했습니다.") from exc


def object_exists(object_path: str) -> bool:
    """버킷에 해당 객체가 있는지 확인한다 (조회 실패는 없음으로 취급)."""
    try:
        return _bucket().blob(object_path).exists()
    except ExternalServiceException:
        raise
    except Exception:
        logger.warning("GCS 객체 존재 확인 실패: %s", object_path)
        return False


def download_file(object_path: str, dest) -> None:
    """버킷 객체를 로컬 파일로 스트리밍 다운로드한다.

    큰 영상을 통째로 메모리에 올리지 않도록 download_bytes 대신 사용한다.
    """
    try:
        _bucket().blob(object_path).download_to_filename(str(dest))
    except ExternalServiceException:
        raise
    except Exception as exc:
        logger.exception("GCS 다운로드 실패: %s", object_path)
        raise ExternalServiceException("영상 저장소에서 영상을 받지 못했습니다.") from exc


def open_object(object_path: str):
    """버킷 객체를 (읽기 스트림, 크기 bytes)로 연다.

    큰 영상을 메모리에 통째로 올리거나 임시 파일로 떨구지 않고 클라이언트에 그대로
    흘려보낼 때 쓴다. 반환한 스트림은 호출자가 닫아야 한다.
    """
    try:
        blob = _bucket().blob(object_path)
        blob.reload()  # size 채우기
        return blob.open("rb"), int(blob.size or 0)
    except ExternalServiceException:
        raise
    except Exception as exc:
        logger.exception("GCS 스트림 열기 실패: %s", object_path)
        raise ExternalServiceException("영상 저장소에서 영상을 받지 못했습니다.") from exc


def delete_object(object_path: str) -> None:
    """버킷에서 객체를 삭제한다. 없으면 조용히 넘어간다."""
    try:
        _bucket().blob(object_path).delete()
    except Exception:
        logger.warning("GCS 객체 삭제 실패(무시): %s", object_path)
