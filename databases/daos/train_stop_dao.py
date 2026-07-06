import logging

from sqlalchemy.orm import Session

from databases.models.train_stop import TrainStop

logger = logging.getLogger(__name__)


def get_stops_for(db: Session, trn_nos: set[str]) -> dict[str, list[TrainStop]]:
    """열차번호 집합의 정차역을 한 번에 조회해 {trn_no: [TrainStop, …](seq 오름차순)}으로 반환.

    경로에 열차가 여러 편이라도 IN 한 방으로 받는다(N+1 방지). soft-delete 제외.
    """
    if not trn_nos:
        return {}
    rows = (
        db.query(TrainStop)
        .filter(
            TrainStop.deleted_at.is_(None),
            TrainStop.trn_no.in_(trn_nos),
        )
        .order_by(TrainStop.trn_no, TrainStop.seq)
        .all()
    )
    out: dict[str, list[TrainStop]] = {}
    for r in rows:
        out.setdefault(r.trn_no, []).append(r)
    return out


def replace_all(db: Session, records: list[dict]) -> int:
    """정차역 테이블을 전량 교체 적재한다(하루치 스냅샷 갱신용). flush만; 커밋은 호출부(스크립트).

    records: {trn_no, seq, stn_cd, stn_nm, stop_se_cd, stop_se_nm, mrnt_nm} dict 리스트.
    참조 데이터라 소프트 삭제가 아닌 하드 삭제 후 재적재한다(station과 같은 성격).
    """
    db.query(TrainStop).delete()
    db.bulk_insert_mappings(TrainStop, records)
    db.flush()
    return len(records)
