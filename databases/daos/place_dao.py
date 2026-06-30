import logging

from sqlalchemy.orm import Session

from core.enums import Theme
from databases.models.place import Place

logger = logging.getLogger(__name__)


def get_candidates(
    db: Session,
    themes: list[Theme] | None = None,
    region: str | None = None,
    limit: int | None = None,
) -> list[Place]:
    """추천 후보 풀을 조회한다(soft-delete 제외).

    themes가 있으면 테마 태그가 하나라도 겹치는 추천지로 필터(GIN 인덱스 `&&`),
    region이 있으면 지역 부분일치로 필터한다. 추천 엔진(recommend/)의 입력.
    """
    q = db.query(Place).filter(Place.deleted_at.is_(None))
    if themes:
        q = q.filter(Place.themes.overlap([t.value for t in themes]))
    if region:
        q = q.filter(Place.region.ilike(f"%{region}%"))
    if limit:
        q = q.limit(limit)
    return q.all()


def get_by_content_ids(db: Session, content_ids: list[str]) -> list[Place]:
    """content_id 목록으로 추천지 조회(필수 경유지 주입용)."""
    if not content_ids:
        return []
    return (
        db.query(Place)
        .filter(Place.deleted_at.is_(None), Place.content_id.in_(content_ids))
        .all()
    )
