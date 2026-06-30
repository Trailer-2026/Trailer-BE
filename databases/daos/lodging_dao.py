import logging

from sqlalchemy.orm import Session

from databases.models.lodging import Lodging

logger = logging.getLogger(__name__)


def get_all(db: Session) -> list[Lodging]:
    """전체 숙소(soft-delete 제외). 규모가 작아(수천) 메모리에서 최근접을 고른다."""
    return (
        db.query(Lodging)
        .filter(Lodging.deleted_at.is_(None), Lodging.lat.isnot(None), Lodging.lng.isnot(None))
        .all()
    )
