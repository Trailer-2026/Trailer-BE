# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Trailer = FastAPI backend (smart train-travel platform). Korean is primary for docs/comments/commits/API descriptions. Python 3.12.

## Commands

- Install: `pip install -r requirements.txt`
- Run: `uvicorn main:app --reload` → Swagger `/docs`, ReDoc `/redoc`
- Notion sync preview (no Notion calls): `python scripts/sync_notion.py --dry-run`
- Per-endpoint extracted errors: `python scripts/error_index.py`
- Train stop-list sync (manual): `python scripts/sync_train_stops.py [YYYYMMDD]` (usually auto — see 열차 정차역 섹션)
- Enable Jira commit hook: `git config core.hooksPath .githooks`
- No tests / linter / formatter configured.

## Layered flow (one module per layer)

`routers/` → `services/` → `databases/daos/` → `databases/models/`

- **routers**: declare endpoint, `Depends(get_db)`, call service, wrap in `CommonResponse`. No business logic.
- **services**: business rules, raise domain exceptions, **own the transaction** — call `db.commit()`.
- **daos**: stateless `(db, ...)` functions. **`db.flush()` only, never commit.**
- **models**: inherit `BaseModel` (`databases/models/base.py`) → `created_at`/`updated_at`/`deleted_at`.

## Invariants (easy to break)

- **Transaction**: only services commit; DAOs flush. `get_db` rolls back on any exception.
- **Soft delete**: delete = set `deleted_at = func.now()`. Every read query MUST filter `deleted_at.is_(None)`.
- **PK naming**: `<table>_idx`.
- **Response envelope**: every endpoint returns `CommonResponse[T]` = `{code, message, data}`. Success: `return CommonResponse.success_response("메시지", data=result)`. Never build error responses manually.
- **Exceptions**: `raise` typed exceptions from `core/exceptions/custom.py` (`NotFoundException` 404 / `BadRequestException` 400 / `UnauthorizedException` 401, all subclass `AppException`). Handlers in `main.py` (`core/exceptions/handlers.py`) convert to the envelope. Global `Exception` handler → Discord alert **only if `is_production()`** + generic 500.

## Auth (`core/security.py`, `routers/auth.py` → `services/auth_service.py` → `utils/oauth.py`)

- Social login only. **Google**: verify OIDC `id_token` (sig/aud/iss) via Google JWKS. **Kakao**: access_token → userinfo endpoint.
- JWT: short access token + refresh token with **rotation + DB whitelist** (`refresh_token` table, keyed by `jti`). Refresh revokes old `jti`, issues new; revoked-token reuse rejected; logout idempotent.
- Protect an endpoint: add `current_user: User = Depends(get_current_user)`.

## Config (`config/properties_dev.ini`, GITIGNORED — must exist locally)

- `[app]` `db.url` (Postgres: `postgresql+psycopg2://...` — README's MySQL example is stale)
- `[jwt]` `secret_key` (+ optional `algorithm` HS256, `access_token_expire_minutes` 60, `refresh_token_expire_days` 14)
- `[oauth]` `google_client_id`
- `[ALARM]` `ENV`, `DISCORD_WEBHOOK_URL` — **injected on server by `deploy.yml` only**, not local.
- Two non-interchangeable readers: `config.Config.read(section, key, default)` (app code) vs `config.external` constants (Discord handler only).

## Notion API-spec automation (follow when adding endpoints)

`scripts/sync_notion.py` extracts spec from OpenAPI + static analysis → Notion (1 endpoint = 1 page). Auto-runs via GitHub Actions on push/merge to **`dev`** (`.github/workflows/sync-notion.yml`); do not run manually. For full extraction:

- **Request**: Pydantic model arg, `Field(..., description="...")` per field.
- **Response payload**: `response_model=CommonResponse[XxxResponse]` (`[None]` if empty).
- **Response message**: pass a **string literal** to `success_response("...")` (extracted statically).
- **Errors**: `raise` a recognized exception reachably from the route. Only `NotFound/BadRequest/Duplicate/Unauthorized` detected; dict-dispatch or `try/except`-swallowed raises are missed → document in route `description`. New exception type → also add HTTP code to `EXC_CODE` in `scripts/error_index.py`.
- Importing the app sets `OPENAPI_EXPORT=1` → `databases/database.py` uses in-memory SQLite (no Postgres).

## 푸시 알림 (FCM)

알림은 **설정**과 **이력** 두 테이블로 나뉜다. 헷갈리지 말 것.

- `notification` — 수신 on/off **설정**, 사용자당 1행(`event_alarm`/`scenery_alarm`). `GET·PATCH /api/users/me/notifications`.
- `notification_log` — 실제로 보낸 알림 **이력**. 앱의 "알림 화면" 목록이 이 테이블이다. `GET /api/notifications`(커서 페이징), `PATCH .../{idx}/read`, `PATCH .../read-all`.

**발송 경로**: 트리거 → `services/push_service.notify()` → 설정 확인(`notification_dao`) → 이력 저장·커밋(`notification_log_dao`) → `services/fcm_service.send_push` → `utils/firebase.send_multicast`.

`notify()`는 **예외를 절대 올리지 않는다**(경고 로그 후 `False`). 알림은 부가 기능이라 호출한 쪽의 트랜잭션·배치 루프를 깨면 안 된다. 그래서 **여행 저장 같은 트리거는 반드시 `db.commit()` 뒤에 호출**한다.

**트리거 3종** (`core.enums.NotificationType`, 설정 매핑은 `push_service._ALARM_FIELD`)

| type | 언제 | 발화 지점 | 설정 | 이력 |
|---|---|---|---|---|
| `TRAVEL_SAVED` | 여행 저장 직후 | `services/travel_service.py` `save_selected_plan`·`create_manual`의 커밋 다음 줄 | `event_alarm` | O |
| `TRAVEL_D1` | 출발 하루 전 KST 자정 **+ 저장 시점에 이미 내일 출발이면 즉시** | `main.py:_trip_reminder_daily_loop` → `trip_reminder_service.send_d1_reminders`, 그리고 `push_service.notify_travel_saved` 안 | `event_alarm` | O |
| `TRAVEL_DELETED` | 여행 삭제 직후 | `services/travel_service.py:delete_travel`의 커밋 다음 줄 | `event_alarm` | O |
| `SCENERY` | 구간별 창밖 관광지 조회 시 | `GET /api/scenic-spots/nearby` → `scenic_spot_service.find_nearby` → `push_service.notify_scenery` | `scenery_alarm` | X |

**유의할 점**

- **풍경은 이력을 남기지 않는다**(`notify(..., record=False)`). 알림 화면에서 풍경은 목록이 아니라 상단 카드로 뜨고 그 카드는 조회 응답(`based_at`·`items`)으로 그리면 되는데, 중복 억제도 없어서 이력을 남기면 아무도 읽지 않는 행이 호출 횟수만큼 쌓이기 때문이다. 결과적으로 `notification_log`에는 여행 알림 3종(`TRAVEL_SAVED`/`TRAVEL_D1`/`TRAVEL_DELETED`)만 들어간다.
- **중복 발송을 막는 건 D-1뿐**이다(`push_service.notify_trip_d1`이 `notification_log_dao.exists`로 직접 검사 — 자정 배치와 저장 시점 두 경로가 같은 여행을 건드리므로 여행당 1회). **풍경은 억제하지 않는다** — 조회할 때마다 보내고, 호출 빈도 조절은 앱 몫이다. 이력을 FCM 호출보다 먼저 커밋하므로 **Firebase 장애 시 재시도하지 않는다(at-most-once)** — 대신 알림 화면에는 남는다.
- **D-1을 저장 시점에도 보내는 이유**: 오늘 저장한 '내일 출발' 여행은 자정 배치만으로는 **영영 알림을 못 받는다**. 다음 자정(= 출발 당일 00:00)의 배치는 '내일 출발'을 찾으므로 그 여행은 이미 대상이 아니기 때문이다.
- **대상은 `travel_idx` 하나**다. 이력에 남는 알림이 전부 여행에 딸린 것이라 다형 참조(`ref_type`/`ref_idx`)를 쓰지 않는다. 딥링크 정보는 FCM `data` payload로도 내려가는데, 거기선 종류에 맞는 키만 담는다(`travel_idx` 또는 풍경의 `scenic_spot_idx`) — FCM `data` 값은 전부 문자열이라 빈 값과 구분이 안 되기 때문이다. `TRAVEL_DELETED`의 `travel_idx`는 이미 삭제된 여행이라 열면 404이니 앱이 이동시키면 안 된다.
- **서버는 사용자의 실시간 위치를 모른다.** 풍경 알림은 앱이 좌표와 탑승 구간을 보내는 `GET /api/scenic-spots/nearby`에 얹혀 있다 — 조회 로직(`scenic_spot_dao.search_on_segment`, 가시 1500m + 진행 방향 ±100°)은 그대로 두고 `scenic_spot_service.find_nearby`가 알림만 덧붙인다. 알림을 보낼 대상이 필요해 **이 엔드포인트는 인증 필수로 바뀌었다**(원래는 인증 없이 호출 가능했다). 응답 스키마는 그대로다. 역명은 `station`·`scenic_spot_segment`와 같은 **"대전역" 형식(`역` 포함)** 이다.
- **문구의 조사**: 여행 제목은 사용자가 자유롭게 지어서 받침 유무가 제각각이라 `push_service._josa_i_ga`로 이/가를 고른다('부산 여행'이 / '제주도'가). 한글로 끝나지 않으면 '가'로 둔다.
- **D-1 루프**는 `train_stop`과 같은 lifespan asyncio 패턴이다. 시작 시 1회 보정 실행(자정에 서버가 꺼져 있었던 경우 복구, 멱등이라 안전) 후 매 자정. `TRIP_REMINDER_AUTOSYNC=0`으로 끌 수 있다(기본 `1`). 다중 워커로 띄워도 이력 검사 덕에 중복 발송은 안 되지만 낭비이므로, 그 땐 끄고 배치를 따로 돌려라.
- **테이블 생성**: 마이그레이션 도구가 없어 `notification`·`notification_log` 둘 다 `push_service.ensure_tables()`(lifespan 1회)로 자체 provision한다 — `train_stop`과 같은 방식. 운영 DB에 수동 DDL을 넣을 필요가 없다.
- **소프트 삭제 예외 아님**: 읽기는 `deleted_at.is_(None)`을 지킨다. 단 `exists`(중복 판정)만은 삭제된 이력도 '보낸 적 있음'으로 세야 재발송을 막을 수 있어 필터하지 않는다.

## 승차권 (두 갈래 — 합치지 않는다)

승차권 데이터가 **두 곳에 따로** 있다. 헷갈리지 말 것.

| | AI 추천 코스 승차권 | 직접 입력 승차권 |
|---|---|---|
| 저장 | `schedule` (kind=train) | `ticket` 테이블 |
| 생기는 시점 | 추천 코스 저장(`POST /api/travels`) 시 | 사용자가 티켓 정보를 입력할 때 |
| 여행 연결 | `travel_idx` 필수 | **없음** — `user_idx`로만 소유 |
| 조회 | `GET /api/travels/{travel_idx}/tickets` | `GET /api/tickets` |
| 열차번호·등급 | 있음 | **없음**(화면에 입력칸이 없다) |
| 호차·좌석 | 직접 입력분만 | 있음(선택) |

**두 소스를 합쳐 내려주지 않는다.** 추천 코스대로 움직이는 사용자는 추천이 준 열차 정보를 그대로 쓰고, `ticket`은 (a) 추천 없이 승차권만 저장하는 사용자와 (b) 추천과 별개로 예매 정보를 적어 두는 사용자를 위한 것이다. 추천을 받았지만 추천과 다른 시간으로 예매한 경우는 다루지 않는다.

**유의할 점**

- **여행에 묶지 않는 이유**: 여행을 하나도 만들지 않은 사용자도 승차권만 저장할 수 있어야 한다. 그래서 `ticket`엔 `travel_idx`가 없고, 여행 기간 검증도 하지 않는다. 검증은 **출발·도착 일시가 엇갈리지 않을 것**과 **출발 일시가 아직 오지 않았을 것** 둘뿐이다(`services/ticket_service.create_ticket`).
- **역은 `station_idx`(FK)로 받는다** — `GET /api/stations`에서 고른 PK. 응답엔 `station`을 조인해 역명("서울역", `역` 접미사 포함)을 함께 담는다. `schedule.dep_station`은 접미사 없는 "서울" 형식이라 표기가 다르다.
- **출발·도착을 Date+Time 4컬럼으로** 나눠 담는다. 화면 입력 단위가 그렇고, 도착일이 출발일과 다를 수 있다(자정 넘김 열차). `travel.start_date`·`schedule.start_time`과 같은 naive KST wall-clock이라 현재 시각 비교 시 `now_kst().replace(tzinfo=None)`으로 tzinfo를 뗀다.
- **테이블 생성**: 마이그레이션 도구가 없어 `ticket_service.ensure_tables()`(lifespan 1회)로 자체 provision한다 — `notification`·`train_stop`과 같은 방식.
- **수정(PATCH)은 없다** — 저장·목록·삭제 3개뿐이다. 잘못 넣었으면 지우고 다시 저장한다.

## 열차 정차역 자동 갱신 (`train_stop`)

경로의 각 열차편(`RouteTrain`)에 **탑승구간 정차역 수·순서**(`stop_station_count`/`stop_stations`)를 붙이는 기능. 데이터는 한국철도공사 **열차운행정보 API**(`travelerTrainRunInfo2`, data.go.kr B551457)에서 온다.

- **적재 경로**: `utils/train_stops.py`(하루치 페이징 페치) → `services/train_stop_service.py`(`refresh`/`refresh_if_stale`, 전량 교체 적재) → `databases/models/train_stop.py`(`train_stop` 테이블) → `databases/daos/train_stop_dao.py`(`get_stops_for` IN 일괄조회·`replace_all`).
- **자동 갱신**: `main.py` lifespan의 백그라운드 태스크(`_train_stop_daily_loop`). 서버 시작 시 데이터가 없거나 20h 넘게 지났으면 1회, 이후 **24h마다**. 실패해도 루프 유지·기존 데이터로 서비스 지속. blocking 작업은 `asyncio.to_thread`로 이벤트 루프를 막지 않음.
- **부착 지점**: `services/recommend_service.py:_attach_train_stops`(`_fetch_routes`에서 `_enrich_stopovers` 다음 호출). 열차번호로 조회해 `_stops_between`이 출발~도착 구간(양끝 포함, `통과` 제외)을 슬라이스. 부가 정보라 조회 실패해도 경로/코스는 그대로.

**유의할 점**

- **다중 워커**: `uvicorn --workers N`/gunicorn으로 여러 프로세스를 띄우면 **워커마다** 갱신이 돈다(전량 교체라 결과는 같지만 낭비·순간 경합). 그 땐 `TRAIN_STOP_AUTOSYNC=0`으로 끄고 **cron/systemd timer로 `scripts/sync_train_stops.py`**를 하루 1회 돌려라. 현재 배포는 단일 `trailer-be` 프로세스라 기본값(자동)으로 둔다.
- **비활성 조건**: 환경변수 `TRAIN_STOP_AUTOSYNC=0`(기본 `1`) 또는 `OPENAPI_EXPORT=1`(노션 동기화·인메모리 SQLite)이면 루프를 안 띄운다.
- **API 보존기간 = 과거~1일 전**(미래 없음). 그래서 `refresh`는 **어제(KST)** 를 조회한다. 정차 패턴은 열차번호별로 안정적이라 미래 여정에 그대로 재사용한다(임시열차·명절 증편만 예외). 빈 응답이면 **기존 데이터를 지우지 않고** 건너뛴다.
- **SRT는 없음**: SRT(수서 출발, SR 운영)는 이 코레일 API에 미수록 → `stop_station_count=null` 폴백. 임시열차·미적재도 동일.
- **매칭 전제**: TAGO `trainno` == 운행정보 `trn_no`(동일 코레일 번호 체계, 검증됨). 역명도 양쪽 "서울/부산"처럼 접미사 없는 동일 형식이라 바로 조인(단 DB `station.station_name`은 "서울역"이라 그쪽 매칭엔 접미사 처리 필요 — `train_stop.stn_nm`은 접미사 없음).
- **전량 교체(하드 삭제)**: `replace_all`은 참조 데이터 스냅샷 갱신이라 소프트 삭제가 아닌 `delete()` 후 재적재다(`station`과 같은 성격 — 소프트삭제 불변식의 의도적 예외). 읽기 DAO는 관례상 `deleted_at.is_(None)`을 유지한다.

## Conventions

- Commits: `<emoji> [Type] 제목` (Feat/Fix/Docs/Refactor/Chore… see README). `.githooks/prepare-commit-msg` auto-prepends `[TRA-NNN]` from branch. Imperative, ≤50 chars, no trailing period.
- **Do NOT `git commit` unless the request explicitly asks.**
- Deploy: push to `main` → `deploy.yml` (SSH GCP VM, pull, install, `systemctl restart trailer-be`).
