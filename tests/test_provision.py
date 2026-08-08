"""스키마 provision 자체 점검 — `python tests/test_provision.py`.

프레임워크 없음(레포에 테스트 설정이 없다) — 깨지면 assert 로 죽는다.

**보는 것 둘**

1. 테이블 생성 순서를 사람이 정하지 않는다. notification_log 가 ticket 을 FK 로 참조하니
   ticket 이 먼저 만들어져야 하는데, 전에는 그 제약이 main.py 의 "순서 주의" 주석에만
   있어서 호출 순서를 바꾸면 조용히 깨졌다. 지금은 create_all 이 FK 그래프를 위상정렬한다
   — 일부러 역순으로 넘겨도 순서가 바로잡히는지 본다.
2. 모델과 provision 상수가 어긋나지 않는다. 모델에만 인덱스를 추가하면 신규 DB 에만
   생기고 운영 DB 엔 영영 안 생긴다(create(checkfirst=True) 는 테이블이 있으면 건너뛴다).

DDL 은 실행하지 않고 Postgres 방언으로 컴파일만 해서 확인한다 — station 이 ARRAY·네이티브
ENUM·생성 컬럼을 써서 SQLite 로는 스키마를 세울 수 없기 때문이다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_mock_engine

from databases import provision
from databases.models.base import Base
# provision 은 reels 를 raw ALTER 로만 건드려 모델을 import 하지 않는다 —
# 메타데이터 대조를 하려면 여기서 올려 둬야 한다.
from databases.models.reels import Reels  # noqa: F401


def _emitted_ddl(tables):
    """주어진 테이블들의 CREATE DDL 을 실행 없이 Postgres 방언으로 뽑는다."""
    statements = []
    engine = create_mock_engine(
        "postgresql+psycopg2://",
        lambda sql, *a, **kw: statements.append(str(sql.compile(dialect=engine.dialect))),
    )
    Base.metadata.create_all(bind=engine, tables=tables, checkfirst=False)
    return statements


def test_fk_order_is_topologically_sorted():
    """_TABLES 를 역순으로 넘겨도 ticket 이 notification_log 보다 먼저 만들어진다."""
    reversed_tables = [model.__table__ for model in reversed(provision._TABLES)]
    assert reversed_tables[0].name == "notification_log", "전제가 깨졌다 — 역순이 아니다"

    creates = [s for s in _emitted_ddl(reversed_tables) if "CREATE TABLE" in s]
    order = [s.split("CREATE TABLE ")[1].split()[0] for s in creates]
    assert order.index("ticket") < order.index("notification_log"), (
        f"notification_log 가 ticket 을 FK 참조하므로 ticket 이 먼저여야 한다: {order}"
    )
    print(f"  OK test_fk_order_is_topologically_sorted (실제 순서: {order})")


def test_create_tables_uses_single_create_all():
    """_TABLES 를 **한 번에** create_all 로 넘겨야 한다 — 하나씩 돌면 순서를 다시 사람이 진다.

    위 test_fk_order_is_topologically_sorted 가 보장하는 건 create_all 의 성질이다.
    provision 이 그걸 실제로 쓰는지는 여기서 본다.
    """
    calls = []

    class FakeMetadata:
        def create_all(self, bind=None, tables=None, checkfirst=None):
            calls.append({"tables": list(tables or []), "checkfirst": checkfirst})

    class FakeBase:
        metadata = FakeMetadata()

    saved = provision.Base
    try:
        provision.Base = FakeBase
        provision._create_tables()
    finally:
        provision.Base = saved

    assert len(calls) == 1, f"create_all 은 1회여야 한다(테이블별 호출 금지): {len(calls)}회"
    assert calls[0]["checkfirst"] is True, "이미 있으면 건너뛰어야 멱등하다"
    passed = {t.name for t in calls[0]["tables"]}
    expected = {m.__table__.name for m in provision._TABLES}
    assert passed == expected, f"_TABLES 전부를 넘겨야 한다: {passed} != {expected}"
    print("  OK test_create_tables_uses_single_create_all")


def test_services_no_longer_own_provisioning():
    """provision 이 유일한 창구다 — 서비스에 ensure_* 가 되살아나면 두 곳이 갈린다."""
    from services import push_service, ticket_service, video_service

    for module, attr in (
        (ticket_service, "ensure_tables"),
        (push_service, "ensure_tables"),
        (push_service, "_ensure_departure_columns"),
        (video_service, "ensure_reels_columns"),
    ):
        assert not hasattr(module, attr), (
            f"{module.__name__}.{attr} 가 살아 있다 — provision 과 이중 관리가 된다"
        )
    assert callable(provision.run)
    print("  OK test_services_no_longer_own_provisioning")


def test_run_orders_stages():
    """run() 은 테이블 → 컬럼 → 인덱스 순이어야 한다.

    새 인덱스가 새 컬럼(notification_log.schedule_idx)을 건드리므로 뒤집으면 깨진다.
    """
    calls = []
    saved = {name: getattr(provision, name)
             for name in ("_create_tables", "_alter_columns", "_create_indexes")}
    try:
        for name in saved:
            setattr(provision, name, (lambda n: lambda: calls.append(n))(name))
        provision.run()
    finally:
        for name, fn in saved.items():
            setattr(provision, name, fn)
    assert calls == ["_create_tables", "_alter_columns", "_create_indexes"], calls
    print("  OK test_run_orders_stages")


def test_added_indexes_match_models():
    """_ADDED_INDEXES 의 인덱스는 모델 __table_args__ 에도 있어야 한다(양쪽이 같아야 한다).

    한쪽에만 있으면 신규 DB 와 운영 DB 의 스키마가 갈린다.
    """
    model_indexes = set()
    for table in Base.metadata.tables.values():
        model_indexes |= {index.name for index in table.indexes}

    for statement in provision._ADDED_INDEXES:
        # "CREATE [UNIQUE] INDEX IF NOT EXISTS <이름> ON ..." 에서 이름만 꺼낸다.
        name = statement.split("IF NOT EXISTS ")[1].split()[0]
        assert name in model_indexes, (
            f"provision 에만 있고 모델엔 없는 인덱스: {name} — 신규 DB 에 안 생긴다"
        )
    print(f"  OK test_added_indexes_match_models ({len(provision._ADDED_INDEXES)}개)")


def test_added_columns_match_models():
    """_ADDED_COLUMNS 의 컬럼은 모델에도 있어야 한다."""
    for table_name, column, _spec in provision._ADDED_COLUMNS:
        table = Base.metadata.tables.get(table_name)
        assert table is not None, f"모델에 없는 테이블: {table_name}"
        assert column in table.c, (
            f"provision 에만 있고 모델엔 없는 컬럼: {table_name}.{column}"
        )
    print(f"  OK test_added_columns_match_models ({len(provision._ADDED_COLUMNS)}개)")


def test_ddl_compiles_for_postgres():
    """ALTER/CREATE INDEX 문이 Postgres 에서 성립하는 형태인지 눈으로 확인한다."""
    for table, column, spec in provision._ADDED_COLUMNS:
        statement = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {spec}"
        assert "IF NOT EXISTS" in statement, statement
        print(f"     {statement}")
    for statement in provision._ADDED_INDEXES:
        assert statement.startswith("CREATE") and "IF NOT EXISTS" in statement, statement
        print(f"     {statement.split(' ON ')[0]} ...")
    print("  OK test_ddl_compiles_for_postgres")


def main():
    test_fk_order_is_topologically_sorted()
    test_create_tables_uses_single_create_all()
    test_services_no_longer_own_provisioning()
    test_run_orders_stages()
    test_added_indexes_match_models()
    test_added_columns_match_models()
    test_ddl_compiles_for_postgres()
    print("provision selfcheck OK")


if __name__ == "__main__":
    main()
