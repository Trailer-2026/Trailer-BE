from sqlalchemy import insert
from sqlalchemy.orm import Session
from databases.models.scenic_spot_segment import ScenicSpotSegment


def bulk_insert(db: Session, mappings: list[dict]) -> None:
    """segment row를 일괄 적재한다. (시드 전용, flush만 — commit은 서비스가 담당)"""
    if not mappings:
        return
    db.execute(insert(ScenicSpotSegment), mappings)
    db.flush()
