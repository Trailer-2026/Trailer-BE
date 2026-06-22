# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Trailer = FastAPI backend (smart train-travel platform). Korean is primary for docs/comments/commits/API descriptions. Python 3.12.

## Commands

- Install: `pip install -r requirements.txt`
- Run: `uvicorn main:app --reload` → Swagger `/docs`, ReDoc `/redoc`
- Notion sync preview (no Notion calls): `python scripts/sync_notion.py --dry-run`
- Per-endpoint extracted errors: `python scripts/error_index.py`
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

## Conventions

- Commits: `<emoji> [Type] 제목` (Feat/Fix/Docs/Refactor/Chore… see README). `.githooks/prepare-commit-msg` auto-prepends `[TRA-NNN]` from branch. Imperative, ≤50 chars, no trailing period.
- **Do NOT `git commit` unless the request explicitly asks.**
- Deploy: push to `main` → `deploy.yml` (SSH GCP VM, pull, install, `systemctl restart trailer-be`).
