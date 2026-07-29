"""GCS(Cloud Storage) 업로드 유틸.

버킷 이름은 [gcs] bucket_name, 인증은 [firebase] credentials(같은 GCP 프로젝트
trailer-7ef0a 의 서비스 계정 키 — Storage 객체 관리자 권한 부여됨)를 재사용한다.
버킷은 공개 읽기(allUsers 뷰어)라 업로드 후 반환하는 URL을 그대로 프론트에 내려준다.
"""
import json
import logging
from functools import lru_cache

from google.cloud import storage
from google.oauth2 import service_account

from config import Config, parser
from core.exceptions.custom import ExternalServiceException

logger = logging.getLogger(__name__)


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


def delete_object(object_path: str) -> None:
    """버킷에서 객체를 삭제한다. 없으면 조용히 넘어간다."""
    try:
        _bucket().blob(object_path).delete()
    except Exception:
        logger.warning("GCS 객체 삭제 실패(무시): %s", object_path)
