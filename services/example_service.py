import logging
from sqlalchemy.orm import Session
from databases.daos import example_dao
from core.exceptions.custom import NotFoundException

logger = logging.getLogger(__name__)


def get_list(skip: int, take: int, db: Session):
    items, total_count = example_dao.get_list(db, skip, take)
    return {"items": items, "total_count": total_count}


def get_detail(example_idx: int, db: Session):
    example = example_dao.get_by_idx(db, example_idx)
    if not example:
        raise NotFoundException("해당 데이터를 찾을 수 없습니다.")
    return example


def create(name: str, description: str, db: Session):
    example = example_dao.create(db, name, description)
    db.commit()
    return example


def update(example_idx: int, name: str, description: str, db: Session):
    example = example_dao.get_by_idx(db, example_idx)
    if not example:
        raise NotFoundException("해당 데이터를 찾을 수 없습니다.")
    example = example_dao.update(db, example, name, description)
    db.commit()
    return example


def delete(example_idx: int, db: Session):
    example = example_dao.get_by_idx(db, example_idx)
    if not example:
        raise NotFoundException("해당 데이터를 찾을 수 없습니다.")
    example_dao.soft_delete(db, example)
    db.commit()
