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
