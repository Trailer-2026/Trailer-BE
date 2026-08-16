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

- `notification` — 수신 on/off **설정**, 사용자당 1행(`event_alarm`/`scenery_alarm`). `GET·PATCH /api/users/me/notifications`. 같은 행에 `marketing_agree`(이벤트·마케팅 활용 동의)도 얹혀 있는데 **발송과 무관한 선택 동의**라 `_ALARM_FIELD`에 넣지 않는다 — 기본값도 알림 둘과 달리 `false`다(안 만진 사용자를 동의한 것으로 볼 수 없다).
- `notification_log` — 실제로 보낸 알림 **이력**. 앱의 "알림 화면" 목록이 이 테이블이다. `GET /api/notifications`(커서 페이징), `PATCH .../{idx}/read`, `PATCH .../read-all`.

**발송 경로**: 트리거 → `services/push_service.notify()` → 설정 확인(`notification_dao`) → 이력 저장·커밋(`notification_log_dao`) → `services/fcm_service.send_push` → `utils/firebase.send_multicast`.

`notify()`는 **예외를 절대 올리지 않는다**(경고 로그 후 `False`). 알림은 부가 기능이라 호출한 쪽의 트랜잭션·배치 루프를 깨면 안 된다. 그래서 **여행 저장 같은 트리거는 반드시 `db.commit()` 뒤에 호출**한다.

**트리거 3종** (`core.enums.NotificationType`, 설정 매핑은 `push_service._ALARM_FIELD`)

| type | 언제 | 발화 지점 | 설정 | 이력 |
|---|---|---|---|---|
| `TRAVEL_SAVED` | 여행 저장 직후 | `services/travel_service.py` `save_selected_plan`·`create_manual`의 커밋 다음 줄 | `event_alarm` | O |
| `TRAVEL_D1` | 출발 하루 전 KST 자정 **+ 저장 시점에 이미 내일 출발이면 즉시** | `main.py:_trip_reminder_daily_loop` → `trip_reminder_service.send_d1_reminders`, 그리고 `push_service.notify_travel_saved` 안 | `event_alarm` | O |
| `TRAVEL_DELETED` | 여행 삭제 직후 | `services/travel_service.py:delete_travel`의 커밋 다음 줄 | `event_alarm` | O |
| `TRAIN_D10M` | 열차 출발 10분 전 | `main.py:_train_departure_loop`(1분 주기) → `train_departure_service.send_departure_reminders` | `event_alarm` | O |
| `SCENERY` | 열차가 풍경 구간에 들어설 때 | `main.py:_scenic_push_loop`(1분 주기) → `scenic_plan_service.send_scenery_reminders` | `scenery_alarm` | O (목록엔 안 뜸) |

**유의할 점**

- **풍경은 이력을 남기되 알림 화면 목록에는 안 뜬다.** 이력을 남기는 목적이 오로지 중복 판정이다 — 발송 시각표를 어디에도 저장하지 않고 1분마다 다시 계산하는 구조라(아래 '풍경 알림' 섹션), 같은 구간을 이미 보냈는지 아는 방법이 `notification_log`뿐이다. 목록에서 빼는 건 `notification_log_dao._HIDDEN_TYPES`가 맡고, 목록과 `unread_count`가 같은 필터를 쓴다(안 그러면 "3건인데 열면 1건"이 생긴다). 화면에서 풍경은 목록의 한 줄이 아니라 상단 카드고, 그 카드는 `GET /api/scenic-spots/plan`(또는 `/nearby`) 응답으로 그린다.
- **중복 발송 억제는 세 종류가 각자 한다.** D-1은 여행당 1회(`push_service.notify_trip_d1` → `notification_log_dao.exists`), 탑승 알림은 출발 1건당 1회, 풍경은 **탑승 1건에서 스팟 1개당 1회**(`notification_log_dao.sent_scenic_spots`). 셋 다 애플리케이션 검사는 빠른 경로일 뿐이고 최종 방어는 부분 유니크 인덱스다. 이력을 FCM 호출보다 먼저 커밋하므로 **Firebase 장애 시 재시도하지 않는다(at-most-once)**.
- **D-1을 저장 시점에도 보내는 이유**: 오늘 저장한 '내일 출발' 여행은 자정 배치만으로는 **영영 알림을 못 받는다**. 다음 자정(= 출발 당일 00:00)의 배치는 '내일 출발'을 찾으므로 그 여행은 이미 대상이 아니기 때문이다.
- **탑승 알림(`TRAIN_D10M`)은 승차권 두 갈래를 모두 훑는다** — 추천 코스(`schedule` kind=train, 출발 일시 = `travel.start_date` + (`day_no`-1)일 + `start_time`)와 직접 입력(`ticket`, `dep_date`+`dep_time`). '두 소스를 합쳐 내려주지 않는다'는 원칙은 조회 API 얘기고, 알림은 양쪽 다 보내야 한다. 이력에도 `schedule_idx`/`ticket_idx`로 구분돼 남는다.
- **중복 방지 축이 종류마다 다르다**: D-1은 여행당 1회(`travel_idx`), 탑승 알림은 **출발 1건당 1회**(`schedule_idx` 또는 `ticket_idx`). 여행 하나에 기차가 여러 번이라 `travel_idx`로 묶으면 환승·귀갓길 알림이 통째로 사라진다. 1분 루프가 10분 창을 열 번 훑으므로 애플리케이션 검사만으론 경합에서 새고, `notification_log`의 부분 유니크 인덱스 둘(`uq_notification_log_train_schedule`·`_ticket`)이 최종 방어다.
- **문구의 '몇 분 뒤'는 상수가 아니라 실제 남은 시간**이다(`_minutes_left`, 올림). 서버가 멈췄다 재개되면 3분 남은 열차에도 알림이 나가는데 '10분 뒤'라고 하면 안 된다. 역명도 출처마다 표기가 달라(`schedule.dep_station`='서울', `ticket`은 조인해 '서울역') `_departure_body`가 '역'을 붙여 맞춘다.
- **`TRAIN_D10M`도 `event_alarm` 스위치를 따른다** — 설정 화면 스위치가 둘(이벤트/풍경)뿐이라 세 번째를 만들면 앱까지 같이 바꿔야 한다. 알림 화면 칩(`title`)도 기존 "일정알림"을 그대로 쓴다.
- **컬럼 provision**: `notification_log`는 이미 운영 DB에 있어서 `create(checkfirst=True)`로는 새 컬럼이 안 생긴다. `databases/provision.py`가 ALTER로 `schedule_idx`·`ticket_idx`와 부분 유니크 인덱스를 덧붙인다. Postgres 전용 구문이라 `OPENAPI_EXPORT=1`(SQLite)에선 호출되지 않는다.
- **루프 끄기**: `TRAIN_DEPARTURE_AUTOSYNC=0`(기본 `1`). 다중 워커면 워커마다 1분 루프가 도는데, 이력 검사 덕에 중복 발송은 안 되지만 낭비다.
- **대상은 종류별 컬럼으로 잡는다**(`travel_idx`/`schedule_idx`/`ticket_idx`) — 다형 참조(`ref_type`/`ref_idx`)는 쓰지 않는다. FK로 무결성을 걸 수 있고 부분 유니크 인덱스로 '한 번만 발송'을 DB가 강제할 수 있어서다. 딥링크 정보는 FCM `data` payload로도 내려가는데, 거기선 종류에 맞는 키만 담는다(`travel_idx`, 직접 입력 승차권의 `ticket_idx`, 풍경의 `scenic_spot_idx`) — FCM `data` 값은 전부 문자열이라 빈 값과 구분이 안 되기 때문이다. 알림 화면 목록 응답도 같은 규칙이라 `travel_idx`와 `ticket_idx`가 함께 있고 둘 중 하나만 채워진다. `TRAVEL_DELETED`의 `travel_idx`는 이미 삭제된 여행이라 열면 404이니 앱이 이동시키면 안 된다.
- **`GET /api/scenic-spots/nearby`는 조회 전용이다** — 푸시를 보내지 않는다. 조회 로직(`scenic_spot_dao.search_on_segment`, 가시 1500m + 진행 방향 ±100°)과 응답 스키마는 그대로고, 인증은 여전히 필수다. 역명은 `station`·`scenic_spot_segment`와 같은 **"대전역" 형식(`역` 포함)** 이다.
- **문구의 조사**: 여행 제목은 사용자가 자유롭게 지어서 받침 유무가 제각각이라 `push_service._josa_i_ga`로 이/가를 고른다('부산 여행'이 / '제주도'가). 한글로 끝나지 않으면 '가'로 둔다.
- **D-1 루프**는 `train_stop`과 같은 lifespan asyncio 패턴이다. 시작 시 1회 보정 실행(자정에 서버가 꺼져 있었던 경우 복구, 멱등이라 안전) 후 매 자정. `TRIP_REMINDER_AUTOSYNC=0`으로 끌 수 있다(기본 `1`). 다중 워커로 띄워도 이력 검사 덕에 중복 발송은 안 되지만 낭비이므로, 그 땐 끄고 배치를 따로 돌려라.
- **테이블 생성**: 마이그레이션 도구가 없어 `notification`·`notification_log` 둘 다 `databases/provision.py`(lifespan 1회)가 만든다. 운영 DB에 수동 DDL을 넣을 필요가 없다.
- **소프트 삭제 예외 아님**: 읽기는 `deleted_at.is_(None)`을 지킨다. 단 `exists`(중복 판정)만은 삭제된 이력도 '보낸 적 있음'으로 세야 재발송을 막을 수 있어 필터하지 않는다.

## 풍경 알림 (시각표를 저장하지 않는다)

`services/scenic_plan_service.py`. 탑승 중인 열차가 **몇 시쯤 어느 풍경 구간을 지나는지** 계산해 그 시각에 푸시한다.

**왜 서버가 보내나.** 전에는 앱이 좌표를 들고 `/nearby`를 폴링할 때만 알림이 나갔다. 그 폴링 구독이 알림 탭 화면 하나에만 걸려 있었고 그 탭은 열어야 켜지는데, 탑승 중 사용자는 일정 탭에 있어서 **발송이 0건**이었다. 발송 주체를 서버로 옮겨 앱이 어느 화면에 있든, 꺼져 있든 나가게 했다. `/nearby`는 조회 전용으로 남았다.

**시각표를 저장하지 않는다 — 새 테이블이 없다.** 계산 입력이 전부 이미 있는 참조 데이터다(`train_stop` 정차 순서, `scenic_spot_segment` 구간별 풍경, `station` 좌표). 게다가 결과가 `(열차번호, 출발역, 도착역)`만으로 정해져 탑승과 무관하므로, **여정 전체를 1로 둔 진행률(0~1)** 로 프로세스 캐시(`_PROFILE_CACHE`, TTL 6h)에 담고 실제 시각은 탑승의 출발·도착 시각에 얹어 만든다. 1분마다 재계산해도 첫 계산 이후 DB를 거의 안 건드린다.

| | |
|---|---|
| 발송 | `main.py:_scenic_push_loop`(1분) → `send_scenery_reminders` → `push_service.notify_scenery` |
| 조회 | `GET /api/scenic-spots/plan` — 지금(또는 3h 내) 탑승의 시각표 전체 |
| 보정 | `POST /api/scenic-spots/plan/calibrate` — 좌표를 보내면 지연만큼 남은 시각을 민다 |
| 중복 방지 | `notification_log` (SCENERY 이력 + 부분 유니크 인덱스 2개) |
| 끄기 | `SCENIC_PUSH_AUTOSYNC=0` (기본 `1`) |

**유의할 점**

- **시각은 어림짐작이다.** 중간역 통과 시각을 주는 데이터가 없어(`train_stop`엔 순서만 있고 시각 컬럼이 없다) 역 간 **직선거리에 비례해** 소요 시간을 나눈다. 정차 시간·가감속·선로 곡률이 전부 빠져 몇 분씩 틀린다. 그래서 문구가 스팟이 아니라 **구간 단위**다("지금 대전역 스팟을 지나고 있어요"). 가시 범위 1500m는 KTX 기준 18초면 지나가는데 그 창을 서버 추정만으로 맞추는 건 불가능하다 — 스팟을 콕 집는 문구로 바꾸려면 GPS 보정을 필수로 만들어야 한다.
- **지연은 GPS로만 잡힌다.** 서버는 열차의 실제 위치를 모른다. 앱이 `calibrate`에 좌표를 보내면 그 좌표를 경로 구간들에 투영해(`utils.scenic.project_on_segment`) '예정대로면 지금 몇 시'를 구하고, 실제 시각과의 차를 그 탑승의 보정값으로 기억한다. **보정 기준은 항상 원래 예정 시각**이라 여러 번 보정해도 값이 누적·발산하지 않는다.
- **보정값은 프로세스 메모리에만 있다**(`_OFFSETS`, TTL 6h). 재기동하면 예정 시각으로 돌아갈 뿐 알림이 끊기지는 않는다. **단일 프로세스 배포를 전제한다** — 다중 워커로 가면 중복 발송은 `notification_log`가 막지만 보정이 워커마다 따로 논다. 그 땐 `SCENIC_PUSH_AUTOSYNC=0`으로 끄고 별도 배치를 돌리거나, 계획을 테이블로 빼야 한다.
- **'운행 중' 판정의 뒤쪽을 열어 둔다**(`_is_riding`). 예정 도착 시각으로 딱 자르면 **지연된 열차의 마지막 구간이 통째로 사라진다** — 20분 늦은 열차는 도착 직전 풍경의 통과 시각도 20분 밀리는데 그 시각엔 이미 '도착한 것'이 되기 때문이다. 그래서 뒤쪽만 보정값 + 발송 창만큼 더 연다.
- **통과역도 시퀀스에 남긴다.** 정차 수를 세는 `recommend_service._stops_between`은 `통과`를 빼지만 여기는 목적이 다르다 — 정차역만 쓰면 KTX처럼 중간을 지나치는 열차는 역 사이가 수십 km라 거리 근사가 거칠어지고, 그 사이 풍경 구간을 앉힐 기준점도 사라진다.
- **구간 매칭은 인접 쌍으로 좁히지 않는다**(`scenic_spot_dao.segments_on_route`). segment가 어느 granularity로 등록돼 있는지 보장이 없어서다 — 인접 쌍이 (서울역, 대전역)인데 segment는 (광명역, 천안아산역)로 등록돼 있으면 인접 매칭으로는 아무것도 못 찾는다. **양끝이 경로에 있기만 하면** 다 가져오고, 역별 진행률로 위치를 계산한다.
- **밀집 구간은 솎는다**(`MIN_GAP_MINUTES`=12). 후보가 그보다 촘촘하면 한 덩어리로 묶어 **구간 직선에서 가장 가까운(=가장 잘 보이는) 하나만** 대표로 보낸다. 솎기가 캐시된 프로필이 아니라 `_timetable`에 있는 이유는 기준이 시간이라서다 — 같은 열차편이라도 여정 길이는 탑승마다 다르다.
- **역명 표기가 세 군데서 다르다.** `train_stop.stn_nm`·`schedule.dep_station`은 "대전"(접미사 없음), `station`·`scenic_spot_segment`·`ticket`(조인)은 "대전역". 이 서비스는 **"대전역" 형식으로 통일**하고 `train_stop`을 볼 때만 떼어 낸다(`_with_suffix`/`_strip_suffix`).
- **직접 입력 승차권은 열차번호를 역추론한다**(`_infer_train_no`). 입력칸이 없어 `train_stop`을 걸 열쇠가 없으니, 출발역·도착역·출발 시각이 맞는 열차를 TAGO 시간표에서 찾는다. 시각은 `TRAIN_MATCH_TOLERANCE_MINUTES`(±2분)만큼 열어 두고 **그 안에서 가장 가까운** 편을 고른다 — 사용자가 표를 보고 손으로 적는 값이라 1~2분 어긋나기 쉬운데, 정확 일치만 보면 그 승차권은 풍경 알림을 통째로, 그것도 아무 신호 없이 못 받는다. 같은 구간에서 2분 안에 연달아 떠나는 열차는 사실상 없다. 실패도 캐시한다(1분마다 공공 API를 두드리지 않기 위해). SRT는 `train_stop`에 없어 풍경 알림도 안 나간다 — 정차역 기능과 같은 한계다.
- **세 저장 경로가 모두 같은 갈래로 들어온다**: AI 추천 코스(`source=RECOMMEND`)·직접 만든 여행(`source=MANUAL`)은 둘 다 `schedule` kind=train이고, 직접 입력 여행도 `travel_service._manual_schedule_fields`가 **열차번호를 필수로 받으므로** 역추론 없이 바로 `train_stop`을 건다. 역명 표기만 갈리는데(`_station_coords`가 '서울'·'서울역' 둘 다 받아 저장 형식이 일정하지 않다) `_with_suffix`/`_strip_suffix`가 흡수한다.
- **발송 창은 10분**(`SEND_WINDOW_MINUTES`). 통과 예정 시각이 지난 뒤 그 안이면 보낸다. 무한정 열지 않는 이유는 몇 시간 꺼져 있었다면 이미 지나간 구간의 "지금 지나고 있어요"가 재기동 때 몰아서 나가기 때문이다.

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
- **테이블 생성**: 마이그레이션 도구가 없어 `databases/provision.py`(lifespan 1회)가 만든다 — `notification`과 같은 곳.
- **날짜 인덱스**: 목록 조회용 `ix_ticket_user_dep`(`user_idx`,`dep_date`,`dep_time`) 말고 `ix_ticket_dep_date`가 따로 있다. 탑승 알림 배치(`ticket_dao.list_departing_on`)는 사용자를 안 가리고 `dep_date`로만 좁히는데, 앞 인덱스는 선두 컬럼이 `user_idx`라 그 조회엔 못 쓴다 — 1분마다 도는 조회라 seq scan이면 비용이 그대로 쌓인다.
- **수정(PATCH)은 없다** — 저장·목록·삭제 3개뿐이다. 잘못 넣었으면 지우고 다시 저장한다.

## 마이페이지 릴스 (북마크 테이블은 없다)

`GET /api/users/me/reels`(내가 올린) / `GET /api/users/me/reels/liked`(좋아요한). 둘 다 `routers/user.py` → `video_service.list_my_reels`·`list_liked_reels` → `reels_dao.list_by_user`·`list_liked_by_user`.

- **북마크 = 릴스 좋아요다.** 앱의 하트가 곧 저장이라 `likes`(reels_idx가 채워진 행)를 그대로 목록으로 읽는다. **별도 북마크 테이블을 만들지 마라** — 하트와 저장이 갈리면 화면에 버튼이 두 개가 되고, `likes`엔 `num_nonnulls(reels_idx, comment_idx) = 1` CHECK가 박혀 있어 세 번째 타깃을 끼우는 것도 막혀 있다(`travel_like`를 따로 뗀 것과 같은 이유).
- **커서가 목록마다 다르다**: 내가 올린 건 `reels_idx`, 좋아요한 건 **`likes_idx`**다. 후자의 정렬 기준이 릴스 생성순이 아니라 내가 누른 순서라 커서도 같은 축이어야 페이지 경계가 안 어긋난다. 그래서 `next_cursor`는 앱이 해석하지 말고 그대로 돌려보내야 한다.
- **응답은 홈 피드 카드와 같은 필드 구성**(`MyReelsItem` ≈ `ReelsRecommendResponse`)이라 같은 카드 컴포넌트로 그린다. 좋아요 목록의 `is_liked`는 항상 true다(목록에서 바로 해제하라고 같이 내려준다).
- **제외 규칙**: 소프트 삭제된 릴스, `url`이 빈 문자열인 렌더 미완료 자리표(피드와 동일), 그리고 좋아요 목록에선 **내가 차단한 사용자의 릴스**(`recommend_reels`와 같은 규칙 — 차단 전에 누른 하트가 남아 그 사람 릴스가 계속 보이면 안 된다).
- `Like.deleted_at`은 안 거른다 — 좋아요 취소가 행 삭제라 소프트 삭제된 좋아요가 없고, `like_dao`의 다른 읽기도 안 건다. 여기서만 거르면 목록엔 없는데 `like_count`엔 잡히는 릴스가 생긴다.
- **`comment_count`는 '내가 볼 수 있는 수'다** — 차단한 사용자의 댓글뿐 아니라 **그 댓글에 달린 남의 답글까지** 뺀다. 댓글 목록은 부모가 차단으로 사라지면 답글도 함께 숨기므로(`comment_service.list_comments`), 작성자만 걸러선 숫자가 목록보다 커진다("댓글 3인데 열면 1개"). 그래서 `comment_dao.counts_by_reels`가 부모 댓글을 self-join해 둘 다 뺀다 — 홈 피드(`recommend_reels`)와 마이페이지 두 목록이 같은 규칙이다. **단 나 자신은 빼지 않는다**: 차단 목록과 달리 '나'는 릴스 노출에서만 제외하는 값이라(`hidden_users`), 그대로 넘기면 내가 쓴 댓글이 안 세어진다.

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
