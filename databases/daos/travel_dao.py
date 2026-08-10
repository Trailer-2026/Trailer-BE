from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session

from databases.models.travel import Travel


def create(
    db: Session, user_idx: int, title: str, start_date: date, end_date: date,
    region: str | None = None, status: str = "PLANNED",
) -> Travel:
    """여행(일정) 1건 생성. flush만 하고 commit은 서비스가 한다."""
    travel = Travel(
        user_idx=user_idx, title=title, start_date=start_date, end_date=end_date,
        region=region, status=status,
    )
    db.add(travel)
    db.flush()
    return travel


def get_by_idx(db: Session, travel_idx: int) -> Travel | None:
    """travel_idx로 단건 조회 (soft-delete 제외)."""
    return db.query(Travel).filter(
        Travel.travel_idx == travel_idx,
        Travel.deleted_at.is_(None),
    ).first()


def get_for_update(db: Session, travel_idx: int) -> Travel | None:
    """travel 행을 FOR UPDATE로 잠가 단건 조회 (soft-delete 제외).

    일정 항목 추가 시 sequence 계산(max+1)→insert를 한 트랜잭션 안에서 직렬화하기 위한 락.
    같은 여행에 대한 동시 추가만 막고(단일 사용자라 경합 미미), 커밋 시 해제된다.
    SQLite(노션 export)에선 FOR UPDATE가 무시돼도 무해하다.
    """
    return db.query(Travel).filter(
        Travel.travel_idx == travel_idx,
        Travel.deleted_at.is_(None),
    ).with_for_update().first()


def update_title(db: Session, travel: Travel, title: str) -> Travel:
    """여행 제목 수정. flush만 하고 commit은 서비스가 한다."""
    travel.title = title
    db.flush()
    return travel


def update_cover_image(db: Session, travel: Travel, url: str | None) -> Travel:
    """대표 사진 URL 지정·해제(None). flush만 하고 commit은 서비스가 한다."""
    travel.cover_image_url = url
    db.flush()
    return travel


def soft_delete(db: Session, travel: Travel) -> None:
    """여행 소프트 삭제 (deleted_at 세팅). flush만, commit은 서비스.

    일정(schedule) 정리는 schedule_dao.soft_delete_by_travel이 맡는다 — DAO는 자기 테이블만 건드린다.
    """
    travel.deleted_at = func.now()
    db.flush()


def list_by_user(db: Session, user_idx: int) -> list[Travel]:
    """사용자의 여행 전체를 시작일 내림차순(최신/예정 먼저)으로 조회 (soft-delete 제외)."""
    return (
        db.query(Travel)
        .filter(Travel.user_idx == user_idx, Travel.deleted_at.is_(None))
        .order_by(Travel.start_date.desc())
        .all()
    )


def get_active_travel(db: Session, user_idx: int, today: date) -> Travel | None:
    """사용자의 '예정/진행 중' 여행 1건 — 종료일이 오늘 이후(포함)인 여행 (soft-delete 제외).

    '예정 여행은 1개만' 정책 검사용. 종료된(오늘 이전) 여행은 지난 여행이라 세지 않는다.
    """
    return (
        db.query(Travel)
        .filter(
            Travel.user_idx == user_idx,
            Travel.deleted_at.is_(None),
            Travel.end_date >= today,
        )
        .order_by(Travel.start_date)
        .first()
    )


def list_starting_on(db: Session, day: date) -> list[Travel]:
    """그 날짜에 출발하는 모든 사용자의 여행 (soft-delete 제외).

    D-1 알림 배치용 — 기준 날짜(KST)는 서비스가 구해 넘긴다.
    """
    return (
        db.query(Travel)
        .filter(
            Travel.deleted_at.is_(None),
            Travel.start_date == day,
        )
        .order_by(Travel.travel_idx)
        .all()
    )


def list_completed_by_user(db: Session, user_idx: int, today: date) -> list[Travel]:
    """사용자의 '지난 여행'(종료일이 오늘 이전) 전체를 최신순으로 조회 (soft-delete 제외).

    status 컬럼은 저장 시 항상 PLANNED이고 전환하는 배치가 없으므로 날짜로만 판정한다.
    오늘(KST)은 서비스가 구해 넘긴다 — DAO는 시간 소스를 갖지 않는다.
    """
    return (
        db.query(Travel)
        .filter(
            Travel.user_idx == user_idx,
            Travel.deleted_at.is_(None),
            Travel.end_date < today,
        )
        .order_by(Travel.end_date.desc(), Travel.travel_idx.desc())
        .all()
    )
