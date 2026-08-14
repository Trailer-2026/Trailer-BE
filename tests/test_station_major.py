# -*- coding: utf-8 -*-
"""대도시 역 후보 조회 자체 점검 — `python tests/test_station_major.py`.

프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.

지키려는 규칙: 후보 티어(KTX → nat_code 보유 → 전체)와 제외 조건(좌표 없음·삭제),
최근접 선택의 정확도, 그리고 **여러 좌표를 훑어도 조회는 1회**라는 것
(도착지 자동 추천이 시도·부분권 수만큼 최근접을 구하므로 여기가 N배가 되면 안 된다).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from databases.daos import station_dao

# station 모델은 Postgres 전용 타입(ENUM 배열·생성 컬럼)을 써서 SQLite 가 만들지 못한다.
# 조회가 읽는 컬럼만 손으로 만든다(test_stamps.py 와 같은 방식). is_ktx 는 운영에선
# 생성 컬럼이지만 여기선 그냥 값으로 넣는다 — DAO 는 읽기만 한다.
_DDL = """
CREATE TABLE station (
  station_idx INTEGER PRIMARY KEY,
  station_name VARCHAR(100),
  nat_code VARCHAR(12),
  latitude FLOAT,
  longitude FLOAT,
  region VARCHAR(50),
  grades TEXT,
  is_ktx BOOLEAN,
  created_at DATETIME,
  updated_at DATETIME,
  deleted_at DATETIME
)
"""

SEOUL = (37.5559, 126.9723)
BUSAN = (35.1151, 129.0403)
DAEJEON = (36.3320, 127.4340)


def _session(rows):
    """rows: (idx, 이름, nat_code, lat, lng, is_ktx, deleted) 튜플 목록."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text(_DDL))
        for idx, name, nat, lat, lng, ktx, deleted in rows:
            conn.execute(
                text("INSERT INTO station (station_idx, station_name, nat_code, latitude,"
                     " longitude, is_ktx, deleted_at) VALUES (:i,:n,:c,:la,:lo,:k,:d)"),
                {"i": idx, "n": name, "c": nat, "la": lat, "lo": lng, "k": ktx, "d": deleted},
            )
    db = sessionmaker(bind=engine)()
    counter = {"selects": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            counter["selects"] += 1

    return db, counter


def _names(stations):
    return sorted(s.station_name for s in stations)


def main() -> None:
    # ── 티어 1: KTX 정차역이 있으면 그것만 후보다 ────────────────────────────────
    db, _ = _session([
        (1, "서울역", "N1", *SEOUL, 1, None),
        (2, "부산역", "N2", *BUSAN, 1, None),
        (3, "간이역", "N3", *DAEJEON, 0, None),      # KTX 아님 → 후보에서 빠진다
    ])
    assert _names(station_dao.major_candidates(db)) == ["부산역", "서울역"]

    # ── 제외 조건: 좌표 없는 역·소프트 삭제된 역은 KTX 여도 빠진다 ───────────────
    db, _ = _session([
        (1, "서울역", "N1", *SEOUL, 1, None),
        (2, "좌표없음", "N2", None, None, 1, None),
        (3, "삭제됨", "N3", *BUSAN, 1, "2026-01-01 00:00:00"),
    ])
    assert _names(station_dao.major_candidates(db)) == ["서울역"]

    # ── 티어 2: KTX 가 하나도 없으면 nat_code 보유역으로 물러선다 ────────────────
    db, _ = _session([
        (1, "일반역", "N1", *SEOUL, 0, None),
        (2, "코드없음", None, *BUSAN, 0, None),
    ])
    assert _names(station_dao.major_candidates(db)) == ["일반역"]

    # ── 티어 3: nat_code 도 없으면 좌표 있는 전체 ────────────────────────────────
    db, _ = _session([(1, "코드없음", None, *BUSAN, 0, None)])
    assert _names(station_dao.major_candidates(db)) == ["코드없음"]

    # 후보가 아예 없으면 빈 목록이고, 최근접도 None 이다(호출부가 이 None 을 걸러낸다).
    db, _ = _session([])
    assert station_dao.major_candidates(db) == []
    assert station_dao.nearest_of([], *SEOUL) is None
    assert station_dao.nearest_major(db, *SEOUL) is None

    # ── 최근접 선택 + 조회 1회 ──────────────────────────────────────────────────
    db, counter = _session([
        (1, "서울역", "N1", *SEOUL, 1, None),
        (2, "부산역", "N2", *BUSAN, 1, None),
        (3, "대전역", "N3", *DAEJEON, 1, None),
    ])
    majors = station_dao.major_candidates(db)
    assert counter["selects"] == 1, counter

    # 좌표를 여러 개 훑어도 추가 조회가 없다 — 이게 이 분리의 목적이다.
    picks = [
        station_dao.nearest_of(majors, 37.60, 126.90).station_name,   # 서울 근처
        station_dao.nearest_of(majors, 35.20, 129.00).station_name,   # 부산 근처
        station_dao.nearest_of(majors, 36.30, 127.40).station_name,   # 대전 근처
    ]
    assert picks == ["서울역", "부산역", "대전역"], picks
    assert counter["selects"] == 1, f"루프에서 재조회가 났다: {counter}"

    # 단발 창구(nearest_major)는 같은 답을 주되 부를 때마다 조회한다 —
    # 그래서 루프에서는 쓰면 안 된다(폴백 경로처럼 1회뿐인 곳에서만).
    before = counter["selects"]
    assert station_dao.nearest_major(db, 35.20, 129.00).station_name == "부산역"
    assert counter["selects"] == before + 1, counter

    print("OK: 대도시 역 후보 조회 자체 점검 통과")


if __name__ == "__main__":
    main()
