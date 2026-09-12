from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from databases.models.train_stop import TrainStop


def latest_created_at(db: Session) -> datetime | None:
    """가장 최근 적재 시각(created_at 최댓값). 없으면 None. 갱신 신선도 판단용.

    train_stop은 하드 삭제(replace_all) 테이블이라 실제 soft-delete 행은 없지만,
    읽기 DAO 관례(전역 불변식)대로 deleted_at 필터를 유지해 get_stops_for와 일관을 맞춘다.
    """
    return (
        db.query(func.max(TrainStop.created_at))
        .filter(TrainStop.deleted_at.is_(None))
        .scalar()
    )


# 열차운행정보 API가 주는 trn_no 는 **5자리 0채움**("00007")인데, 부르는 쪽이 들고 오는
# 번호는 TAGO 열차정보의 trainno 라 0이 없다("7", "101"). 같은 코레일 번호 체계인데
# 표기만 다르다. 저장은 API가 준 그대로 두고(재적재 때 흔들리지 않게), 조회에서 맞춘다.
_TRN_NO_WIDTH = 5


def _padded(trn_no: str) -> str:
    """열차번호를 train_stop 의 표기(5자리 0채움)로 맞춘다. 숫자가 아니면 그대로 둔다."""
    trn_no = (trn_no or "").strip()
    return trn_no.zfill(_TRN_NO_WIDTH) if trn_no.isdigit() else trn_no


def get_stops_for(db: Session, trn_nos: set[str]) -> dict[str, list[TrainStop]]:
    """열차번호 집합의 정차역을 한 번에 조회해 {trn_no: [TrainStop, …](seq 오름차순)}으로 반환.

    경로에 열차가 여러 편이라도 IN 한 방으로 받는다(N+1 방지). soft-delete 제외.

    **키는 부르는 쪽이 준 표기 그대로 돌려준다** — 조회할 때만 0채움으로 맞추고(_padded),
    돌려줄 때 원래 표기로 되돌린다. 호출부는 `schedule.train_no`·TAGO `trainno` 를 그대로
    들고 와서 그대로 찾아 쓰므로, 여기서 표기를 바꿔 내보내면 `stops_map.get(t.train_no)` 가
    전부 빗나간다. 표기 차이를 아는 곳을 이 함수 하나로 가둔다.
    """
    if not trn_nos:
        return {}
    # 한 요청에 "7"과 "00007"이 함께 올 수 있다(추천 경로와 저장된 일정이 섞이는 경우).
    # 0채움 표기 하나당 원래 표기 여럿이 달릴 수 있어 리스트로 받는다.
    originals: dict[str, list[str]] = {}
    for trn_no in trn_nos:
        originals.setdefault(_padded(trn_no), []).append(trn_no)

    rows = (
        db.query(TrainStop)
        .filter(
            TrainStop.deleted_at.is_(None),
            TrainStop.trn_no.in_(originals),
        )
        .order_by(TrainStop.trn_no, TrainStop.seq)
        .all()
    )
    out: dict[str, list[TrainStop]] = {}
    for r in rows:
        for original in originals.get(r.trn_no, ()):
            out.setdefault(original, []).append(r)
    return out


def all_sequences(db: Session) -> list[tuple[str, int, str]]:
    """전 열차의 (열차번호, 정차순서, 역명)을 seq 오름차순으로. 정차 연결 인덱스 구축용.

    전량(수천 행)을 한 번에 읽지만 컬럼 3개뿐이라 가볍고, 호출부가 결과를 캐시한다.
    """
    return (
        db.query(TrainStop.trn_no, TrainStop.seq, TrainStop.stn_nm)
        .filter(TrainStop.deleted_at.is_(None))
        .order_by(TrainStop.trn_no, TrainStop.seq)
        .all()
    )


def replace_all(db: Session, records: list[dict]) -> int:
    """정차역 테이블을 전량 교체 적재한다(하루치 스냅샷 갱신용). flush만; 커밋은 호출부(스크립트).

    records: {trn_no, seq, stn_cd, stn_nm, stop_se_cd, stop_se_nm, mrnt_nm} dict 리스트.
    참조 데이터라 소프트 삭제가 아닌 하드 삭제 후 재적재한다(station과 같은 성격).
    """
    db.query(TrainStop).delete()
    db.bulk_insert_mappings(TrainStop, records)
    db.flush()
    return len(records)


def stops_between(db: Session, dep: str, arr: str) -> list[str]:
    """두 역을 모두 서는 **모든** 열차의 사이 정차역을 합쳐 반환(양끝 포함, 중복 제거).

    창밖 풍경 조회(`/api/scenic-spots/nearby`)가 쓴다. 그쪽은 탑승 구간의 **양 끝**만 알고
    열차번호를 모르는데, 풍경 구간(scenic_spot_segment)은 인접역 쌍으로 등록돼 있어
    (서울역, 대전역) 같은 양끝 쌍으로는 하나도 안 걸린다. 사이 역을 펴 줘야 매칭된다.

    **열차 하나만 고르면 안 된다.** 같은 두 역을 잇는 선로가 여럿이라(서울~대전은 경부선
    일반과 고속선이 나란히 간다) 어느 하나를 집으면 다른 선로의 풍경 구간을 통째로 놓친다
    — 정차역이 가장 많은 열차를 골랐더니 일반선(수원·오산·평택)이 잡혀, 고속선(광명·
    천안아산·오송)에 등록된 341곳이 전부 빠졌다. 그래서 합집합으로 낸다.

    합집합이라 사용자가 안 타는 선로의 구간도 섞이지만, 호출측이 **현재 좌표에서 가시거리
    1500m·진행 방향 ±100°**로 다시 거르므로 실제로 곁에 있는 것만 남는다.

    역 표기는 train_stop 쪽(접미사 없음, '대전')이다 — 호출측이 '역'을 붙여 쓴다.
    """
    dep, arr = _strip(dep), _strip(arr)
    if not dep or not arr or dep == arr:
        return []
    both = (
        db.query(TrainStop.trn_no)
        .filter(TrainStop.deleted_at.is_(None), TrainStop.stn_nm.in_((dep, arr)))
        .group_by(TrainStop.trn_no)
        .having(func.count(func.distinct(TrainStop.stn_nm)) == 2)
        .subquery()
    )
    rows = (
        db.query(TrainStop)
        .filter(TrainStop.deleted_at.is_(None), TrainStop.trn_no.in_(db.query(both.c.trn_no)))
        .order_by(TrainStop.trn_no, TrainStop.seq)
        .all()
    )
    by_train: dict[str, list[str]] = {}
    for r in rows:
        by_train.setdefault(r.trn_no, []).append(r.stn_nm)

    out: list[str] = []
    seen: set[str] = set()
    for names in by_train.values():
        try:
            i, j = names.index(dep), names.index(arr)
        except ValueError:
            continue
        for name in (names[i:j + 1] if i <= j else names[j:i + 1][::-1]):
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _strip(name: str) -> str:
    """'대전역' → '대전' (train_stop 표기)."""
    name = (name or "").strip()
    return name[:-1] if len(name) > 1 and name.endswith("역") else name
