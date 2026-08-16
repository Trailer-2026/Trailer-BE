"""서버 기동 시 1회 도는 스키마 provision — 마이그레이션 도구가 없어 앱이 직접 챙긴다.

이 저장소엔 Alembic 같은 마이그레이션 도구가 없다. 그래서 새 테이블·새 컬럼·새 인덱스는
서버가 뜰 때 스스로 만든다(main.py lifespan에서 `run()` 1회). 운영 DB에 수동 DDL을 넣지
않아도 배포만으로 스키마가 맞춰진다.

**왜 한 곳에 모았나**: 전에는 여러 서비스가 각자 ensure_*
함수를 들고 있었고 main.py가 그 셋을 "순서 주의" 주석과 함께 차례로 불렀다. notification_log가
ticket을 FK로 참조하니 ticket이 먼저 있어야 하는데 그 제약이 코드가 아니라 주석에만 있어서,
호출 순서를 바꾸면 조용히 깨졌다. 지금은 `_TABLES`에 모델만 넣어 두면 `create_all`이 FK
그래프를 위상정렬해 부모 테이블부터 만든다 — 순서를 사람이 기억할 필요가 없다.

세 단계로 나뉘고, 단계 사이의 순서는 `run()`이 코드로 강제한다.
  1) `_create_tables`  — 없는 테이블 생성
  2) `_alter_columns`  — **이미 있는** 테이블에 나중에 추가된 컬럼 덧붙이기
  3) `_create_indexes` — **이미 있는** 테이블에 나중에 추가된 인덱스 덧붙이기

2·3이 따로 있는 이유: `create(checkfirst=True)`는 테이블이 이미 있으면 통째로 건너뛴다.
운영 DB엔 notification_log·reels·ticket이 이미 있어서, 뒤에 추가된 컬럼·인덱스는 여기서
ALTER/CREATE INDEX로 따로 챙기지 않으면 영영 생기지 않는다. 3이 2보다 뒤인 것도 필수다 —
새 인덱스가 새 컬럼(notification_log.schedule_idx)을 건드린다.

전부 멱등이라(checkfirst / IF NOT EXISTS) 몇 번을 돌려도 안전하다. Postgres 전용 구문을
쓰므로 OPENAPI_EXPORT=1(인메모리 SQLite)일 땐 main.py가 아예 호출하지 않는다.

train_stop만 여기 없다 — standalone 스크립트(scripts/sync_train_stops.py)도 그 테이블을
필요로 해서 lifespan이 아니라 train_stop_service 안에서 챙긴다.
"""
from sqlalchemy import text

from databases.database import engine
from databases.models.base import Base

# FK 대상까지 같은 MetaData에 올라와 있어야 DDL이 컴파일된다(NoReferencedTableError 방지).
# create_all이 이 등록 정보로 의존 순서를 정하므로 import 순서 자체는 의미가 없다.
from databases.models.ban import Ban
from databases.models.notification import Notification
from databases.models.notification_log import NotificationLog
from databases.models.schedule import Schedule  # noqa: F401  notification_log의 FK 대상
from databases.models.scenic_spot import ScenicSpot  # noqa: F401  notification_log의 FK 대상
from databases.models.scenic_spot_segment import ScenicSpotSegment  # noqa: F401  ScenicSpot의 relationship 대상
from databases.models.station import Station  # noqa: F401  ticket의 FK 대상
from databases.models.ticket import Ticket
from databases.models.travel import Travel  # noqa: F401  notification_log의 FK 대상
from databases.models.travel_image import TravelImage
from databases.models.travel_like import TravelLike
from databases.models.user import User  # noqa: F401  모든 테이블의 FK 대상
from databases.models.user_stamp import UserStamp

# 없으면 만들 테이블. **순서는 신경 쓰지 않는다** — create_all이 FK 의존 그래프를
# 위상정렬해 부모부터 만든다(ticket·travel·schedule → notification_log).
# 모델을 새로 추가했으면 반드시 여기 넣어라 — 운영 DB엔 손으로 만들어 뒀더라도, 빠져 있으면
# 새 환경(로컬·재구축)에서 그 기능이 통째로 500이 된다(ban·travel_like가 그랬다).
_TABLES = (Ticket, Notification, NotificationLog, UserStamp, TravelImage, Ban, TravelLike)

# 이미 있는 테이블에 덧붙일 컬럼 — (테이블, 컬럼명, 타입·제약).
# 모델 정의와 같은 내용이어야 한다. 새 컬럼을 모델에 넣었다면 여기에도 넣어라.
_ADDED_COLUMNS = (
    # 탑승 알림(TRAIN_D10M)이 어느 승차권에서 나왔는지 — 추천 코스 / 직접 입력.
    ("notification_log", "schedule_idx", "INTEGER REFERENCES schedule(schedule_idx)"),
    ("notification_log", "ticket_idx", "INTEGER REFERENCES ticket(ticket_idx)"),
    # 홈 화면 릴스 카드용 지역 태그·대표 프레임.
    ("reels", "region", "VARCHAR(50)"),
    ("reels", "thumbnail_url", "VARCHAR(200)"),
    # 사용자가 지정한 여행 대표 사진(직접 만든 여행은 일정 이미지가 없어 썸네일이 빈다).
    ("travel", "cover_image_url", "VARCHAR(255)"),
    # 저장 출처(RECOMMEND | MANUAL) — 'AI 추천 코스로 여행 완료' 스탬프 판정용.
    ("travel", "source", "VARCHAR(20)"),
    # 스탬프 획득 알림이 어느 칸인지 — FK가 아니라 종류 문자열이다(notification_log 참조).
    ("notification_log", "stamp_type", "VARCHAR(30)"),
    # 풍경 알림이 어느 스팟이었는지 — schedule_idx/ticket_idx와 묶여 중복 판정 키가 된다.
    (
        "notification_log", "scenic_spot_idx",
        "INTEGER REFERENCES scenic_spot(scenic_spot_idx)",
    ),
    # 이벤트·마케팅 활용 동의. 알림 두 컬럼과 달리 기본값이 false다 — 선택 동의라
    # 이미 설정 행이 있는 사용자를 동의한 것으로 채우면 안 된다.
    ("notification", "marketing_agree", "BOOLEAN NOT NULL DEFAULT false"),
    # 사용자가 올린 여행 사진의 소유 여행. 일정(schedule_idx)을 고르지 않고 올릴 수 있게
    # 되면서 필요해졌다 — 여기서는 NULL 허용으로 붙이고, 아래 _FIXUPS가 채운 뒤 NOT NULL로 조인다.
    ("travel_image", "travel_idx", "INTEGER REFERENCES travel(travel_idx)"),
    # travel_image가 BaseModel을 상속하면서 생긴 감사 컬럼. 테이블이 이미 운영 DB에 있어
    # create_all(checkfirst=True)로는 안 붙는다. deleted_at이 없으면 삭제 API가 깨진다.
    ("travel_image", "created_at", "TIMESTAMPTZ DEFAULT now()"),
    ("travel_image", "updated_at", "TIMESTAMPTZ"),
    ("travel_image", "deleted_at", "TIMESTAMPTZ"),
)

# ADD COLUMN 으로 안 되는 뒷정리 — 기존 행 백필·제약 변경. _alter_columns 다음에 돈다.
# 전부 멱등이라(WHERE ... IS NULL / 이미 적용된 제약은 no-op) 매 기동마다 돌아도 안전하다.
# 단 ADD CONSTRAINT 만은 Postgres에 IF NOT EXISTS 가 없어 스스로 멱등해지지 않는다 —
# DO 블록으로 duplicate_object 를 삼켜 두 번째 기동부터 no-op 이 되게 한다.
_FIXUPS = (
    # 사진이 붙은 일정에서 여행을 끌어와 옛 행을 채운다. 이게 없으면 travel_idx가 NULL로
    # 남아 여행 단위 조회에서 그 사진들이 통째로 사라진다(FK가 있어 조인은 항상 성공한다).
    "UPDATE travel_image ti SET travel_idx = s.travel_idx "
    "FROM schedule s WHERE s.schedule_idx = ti.schedule_idx AND ti.travel_idx IS NULL",
    # 백필이 끝난 뒤에야 조일 수 있다(모델의 nullable=False와 일치시킨다).
    "ALTER TABLE travel_image ALTER COLUMN travel_idx SET NOT NULL",
    # 일정을 고르지 않고 여행에만 올린 사진을 담으려면 NOT NULL을 풀어야 한다.
    "ALTER TABLE travel_image ALTER COLUMN schedule_idx DROP NOT NULL",
    # 풍경 이력의 참조 XOR — 모델 __table_args__의 ck_notification_log_scenery_ref와 같은 정의.
    # 아래 uq_notification_log_scenery_* 두 인덱스가 중복 발송의 최종 방어인데, 인덱스 컬럼에
    # NULL이 섞이면 유니크가 서로를 구별하지 못해 방어가 조용히 풀린다(모델 쪽 주석 참조).
    #
    # NOT VALID인 이유: 기존 행을 스캔하지 않는다. 이 제약의 목적은 앞으로 들어올 행을 막는
    # 것이고 그건 NOT VALID로도 완전히 지켜진다(이후 모든 INSERT/UPDATE에 적용된다). 반면
    # 검증까지 켜면 위반 행이 하나라도 있을 때 ALTER가 실패하고, 이 코드는 lifespan에서
    # 도는 부팅 경로라 **서버가 아예 안 뜬다**. 방어용 제약이 기동을 막을 수 있게 두는 건
    # 득보다 실이 크다. (풍경 이력은 record=False였던 TRA-50~77 동안 한 행도 쌓이지 않았고
    # scenic_spot_idx 컬럼 자체가 TRA-78에 생겨서, 실제로 위반 행이 있을 가능성은 낮다.
    # 확인 후 조이려면 ALTER TABLE notification_log VALIDATE CONSTRAINT ... 를 한 번 돌려라.)
    "DO $$ BEGIN "
    "ALTER TABLE notification_log ADD CONSTRAINT ck_notification_log_scenery_ref "
    "CHECK (type <> 'SCENERY' OR (scenic_spot_idx IS NOT NULL "
    "AND (schedule_idx IS NULL) <> (ticket_idx IS NULL))) NOT VALID; "
    "EXCEPTION WHEN duplicate_object THEN NULL; END $$",
)

# 이미 있는 테이블에 덧붙일 인덱스. 모델 __table_args__의 같은 인덱스와 정의가 일치해야 한다.
_ADDED_INDEXES = (
    # 탑승 알림 '출발 1건당 1회'의 최종 방어. 1분 루프가 10분 창을 열 번 훑으므로
    # 애플리케이션 검사(check-then-act)만으로는 경합에서 샌다. 출처가 둘이라 인덱스도 둘 —
    # 한 행은 schedule_idx/ticket_idx 중 하나만 채우고, NULL은 유니크 제약에서 서로 구별된다.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_log_train_schedule "
    "ON notification_log (user_idx, schedule_idx) WHERE type = 'TRAIN_D10M'",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_log_train_ticket "
    "ON notification_log (user_idx, ticket_idx) WHERE type = 'TRAIN_D10M'",
    # 풍경 알림 '탑승 1건에서 스팟 1개당 1회'의 최종 방어. 발송 시각표를 저장하지 않고
    # 1분마다 재계산하는 구조라 같은 구간이 발송 창 안에서 여러 번 잡힌다.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_log_scenery_schedule "
    "ON notification_log (user_idx, schedule_idx, scenic_spot_idx) WHERE type = 'SCENERY'",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_log_scenery_ticket "
    "ON notification_log (user_idx, ticket_idx, scenic_spot_idx) WHERE type = 'SCENERY'",
    # 여행 상세·영상 렌더가 여행 단위로 사진을 훑는다(travel_image.travel_idx index=True와 같은 이름).
    "CREATE INDEX IF NOT EXISTS ix_travel_image_travel_idx ON travel_image (travel_idx)",
    # 탑승 알림 배치가 1분마다 도는 조회(ticket_dao.list_departing_on: dep_date IN (...))용.
    # 기존 ix_ticket_user_dep는 선두 컬럼이 user_idx라, 사용자를 가리지 않는 이 조회는
    # 그 인덱스를 못 타고 seq scan으로 떨어진다.
    "CREATE INDEX IF NOT EXISTS ix_ticket_dep_date ON ticket (dep_date)",
    # 스탬프 자정 배치가 '그 기간에 여행이 끝난 사용자'를 고르는 조회용. 사용자를 안 가리는
    # 조회라 선두 컬럼이 user_idx인 기존 인덱스를 못 타고 seq scan으로 떨어진다.
    "CREATE INDEX IF NOT EXISTS ix_travel_end_date ON travel (end_date)",
)


def run() -> None:
    """스키마를 현재 코드에 맞춘다. 서버 기동 시 1회(main.py lifespan). 멱등하다.

    네 단계의 호출 순서가 곧 의존 관계다 — 컬럼을 붙이려면 테이블이 있어야 하고,
    백필·제약 변경은 컬럼이 있어야 하고, 인덱스를 걸려면 컬럼이 있어야 한다.
    각 단계는 자기 트랜잭션에서 커밋한다.
    """
    _create_tables()
    _alter_columns()
    _apply_fixups()
    _create_indexes()


def _create_tables() -> None:
    """없는 테이블을 만든다. 이미 있으면 손대지 않는다(그래서 _alter_columns가 필요하다)."""
    Base.metadata.create_all(
        bind=engine, tables=[model.__table__ for model in _TABLES], checkfirst=True
    )


def _alter_columns() -> None:
    """이미 만들어져 있는 테이블에 나중에 추가된 컬럼을 덧붙인다."""
    with engine.begin() as conn:
        for table, column, spec in _ADDED_COLUMNS:
            conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {spec}")
            )


def _apply_fixups() -> None:
    """ADD COLUMN으로 안 되는 뒷정리(기존 행 백필·제약 변경)를 순서대로 적용한다.

    문장 하나가 실패하면 전부 롤백된다 — 백필 없이 NOT NULL만 걸리는 어중간한 상태를
    만들지 않기 위해서다(그 상태로는 새 사진 INSERT가 전부 깨진다).
    """
    with engine.begin() as conn:
        for statement in _FIXUPS:
            conn.execute(text(statement))


def _create_indexes() -> None:
    """이미 만들어져 있는 테이블에 나중에 추가된 인덱스를 덧붙인다."""
    with engine.begin() as conn:
        for statement in _ADDED_INDEXES:
            conn.execute(text(statement))
