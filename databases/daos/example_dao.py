import logging
from sqlalchemy.orm import Session
from sqlalchemy.sql import func
from databases.models.example import Example

logger = logging.getLogger(__name__)


def get_list(db: Session, skip: int = 0, take: int = 10):
    query = db.query(Example).filter(Example.deleted_at.is_(None))
    total_count = query.count()
    items = query.order_by(Example.example_idx.desc()).offset(skip).limit(take).all()
    return items, total_count


def get_by_idx(db: Session, example_idx: int):
    return db.query(Example).filter(
        Example.example_idx == example_idx,
        Example.deleted_at.is_(None)
    ).first()


def create(db: Session, name: str, description: str = None):
    example = Example(name=name, description=description)
    db.add(example)
    db.flush()
    return example


def update(db: Session, example: Example, name: str = None, description: str = None):
    if name is not None:
        example.name = name
    if description is not None:
        example.description = description
    db.flush()
    return example


def soft_delete(db: Session, example: Example):
    example.deleted_at = func.now()
    db.flush()
