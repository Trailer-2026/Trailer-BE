# -*- coding: utf-8 -*-
"""OpenAPI 명세 → Notion DB 동기화.

FastAPI 의 app.openapi() 에서 엔드포인트를 추출해 Notion 데이터베이스에
엔드포인트 1개 = row(page) 1개 로 upsert 한다. 각 페이지 본문에는 Request 스키마,
Response 규격, Error 응답을 정리한다. 코드에서 사라진 엔드포인트의 row 는
archive(보관) 처리한다.

서버를 띄우지 않고 코드에서 직접 스펙을 뽑는다. main.py 의 import 부작용
(Firebase init, 슈퍼관리자 시드)을 피하려고 OPENAPI_EXPORT=1 을 먼저 세팅한다.

응답/에러 한계: 라우트에 response_model / responses 가 선언돼 있지 않아 OpenAPI 가
응답 본문·커스텀 에러를 모른다. 따라서 Response 는 공통 엔벨로프({code,message,data}),
Error 는 공통 카탈로그로 문서화한다(요청 스키마만 엔드포인트별 정밀).

사용법:
  # 최초 1회 — 부모 페이지 밑에 DB 를 만들고 database_id 를 출력
  NOTION_TOKEN=xxx NOTION_PARENT_PAGE_ID=xxx python scripts/sync_notion.py --init-db
  # 동기화 — upsert + 본문 갱신
  NOTION_TOKEN=xxx NOTION_DATABASE_ID=xxx python scripts/sync_notion.py
  # Notion 호출 없이 추출만 확인
  python scripts/sync_notion.py --dry-run

환경변수:
  NOTION_TOKEN          (필수) Notion Integration 토큰
  NOTION_DATABASE_ID    (동기화 시 필수) 대상 데이터베이스 id
  NOTION_PARENT_PAGE_ID (--init-db 시 필수) DB 를 만들 부모 페이지 id
"""
import os
import sys
import time

# 프로젝트 루트를 import 경로에 추가 (scripts/ 에서 실행해도 main 을 찾도록)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# main 을 import 하기 전에 부작용 차단 (DB/Firebase 미연결)
os.environ.setdefault("OPENAPI_EXPORT", "1")

import requests  # noqa: E402

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
RICH_TEXT_LIMIT = 2000  # Notion rich_text 단일 텍스트 길이 상한
CHILDREN_LIMIT = 100  # children 블록 1회 추가 상한
THROTTLE_SEC = 0.34  # Notion rate limit(평균 3 req/s) 회피용

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head")

# 프로젝트 공통 에러 카탈로그 (core/exceptions/custom.py 기준) — 해당 엔드포인트에서
# 발생 가능한 경우 표시되는 공통 규격. 라우트에 선언돼 있지 않아 자동 추출 불가.
COMMON_ERRORS = [
    ("400", "BadRequestException", "잘못된 요청(검증 실패·비즈니스 규칙 위반)"),
    ("401", "UnauthorizedException", "인증 실패(로그인 정보 불일치)"),
    ("404", "NotFoundException", "리소스를 찾을 수 없음"),
    ("409", "DuplicateException", "중복(이미 존재하는 리소스)"),
    ("422", "ValidationError", "요청 본문/파라미터 형식 오류 (FastAPI 기본)"),
    ("500", "Internal Server Error", "서버 내부 오류"),
]


# ---------------------------------------------------------------------------
# Notion HTTP (rate-limit 재시도 + 스로틀)
# ---------------------------------------------------------------------------

def _headers():
    token = os.getenv("NOTION_TOKEN")
    if not token:
        sys.exit("ERROR: NOTION_TOKEN 환경변수가 필요합니다.")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _request(method: str, url: str, **kwargs):
    """429(rate limit)·네트워크 오류(timeout/connection) 시 백오프 후 재시도."""
    res = None
    for attempt in range(6):
        time.sleep(THROTTLE_SEC)
        try:
            res = requests.request(method, url, headers=_headers(), timeout=30, **kwargs)
        except requests.exceptions.RequestException as e:
            wait = min(2 ** attempt, 10)
            print(f"  ! 네트워크 오류({type(e).__name__}) — {wait}s 후 재시도 ({attempt + 1}/6)")
            time.sleep(wait)
            continue
        if res.status_code == 429:
            wait = float(res.headers.get("Retry-After", "1"))
            time.sleep(wait)
            continue
        return res
    if res is None:
        sys.exit(f"ERROR: Notion 요청 실패(네트워크) {method} {url}")
    return res  # 마지막 응답 그대로 반환(호출부에서 에러 처리)


# ---------------------------------------------------------------------------
# rich_text / 블록 헬퍼
# ---------------------------------------------------------------------------

def _truncate(text: str) -> str:
    text = text or ""
    return text if len(text) <= RICH_TEXT_LIMIT else text[: RICH_TEXT_LIMIT - 1] + "…"


def _rt(value: str):
    """rich_text 배열 (단일 텍스트)."""
    return [{"type": "text", "text": {"content": _truncate(value)}}] if value else []


def _rich_text(value: str):
    return {"rich_text": _rt(value)}


def _title(value: str):
    return {"title": _rt(value)}


def _select(value: str):
    return {"select": {"name": value}} if value else {"select": None}


def _h2(text):
    return {"type": "heading_2", "heading_2": {"rich_text": _rt(text)}}


def _h3(text):
    return {"type": "heading_3", "heading_3": {"rich_text": _rt(text)}}


def _para(text):
    return {"type": "paragraph", "paragraph": {"rich_text": _rt(text)}}


def _code(text, language="json"):
    return {"type": "code", "code": {"rich_text": _rt(text), "language": language}}


def _bullet(text):
    return {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rt(text)}}


def _table(headers: list[str], rows: list[list[str]]):
    """간단한 Notion 테이블 블록."""
    width = len(headers)
    children = [{"type": "table_row", "table_row": {"cells": [_rt(h) for h in headers]}}]
    for row in rows:
        cells = [_rt(str(c)) for c in row]
        cells += [_rt("")] * (width - len(cells))  # 폭 보정
        children.append({"type": "table_row", "table_row": {"cells": cells[:width]}})
    return {
        "type": "table",
        "table": {"table_width": width, "has_column_header": True, "children": children},
    }


# ---------------------------------------------------------------------------
# OpenAPI 스키마 → 타입 문자열 / 필드 펼치기
# ---------------------------------------------------------------------------

def _type_str(prop: dict) -> str:
    if not isinstance(prop, dict):
        return ""
    if "$ref" in prop:
        return prop["$ref"].rsplit("/", 1)[-1]
    if "anyOf" in prop:
        parts = [_type_str(p) for p in prop["anyOf"]]
        non_null = [p for p in parts if p != "null"]
        base = " | ".join(non_null) or "any"
        return f"{base} (nullable)" if "null" in parts else base
    if "allOf" in prop:
        return " & ".join(_type_str(p) for p in prop["allOf"])
    if "enum" in prop:
        return "enum: " + " | ".join(map(str, prop["enum"]))
    t = prop.get("type")
    if t == "array":
        return f"array<{_type_str(prop.get('items', {}))}>"
    if t:
        fmt = prop.get("format")
        return f"{t}({fmt})" if fmt else t
    return "object"


def _expand_schema(ref_name: str, schemas: dict) -> list[list[str]]:
    """스키마 이름 → [[필드, 타입, 필수, 설명], ...]."""
    schema = schemas.get(ref_name, {})
    required = set(schema.get("required", []))
    rows = []
    for name, prop in schema.get("properties", {}).items():
        desc = prop.get("description", "") or ("기본값: %s" % prop["default"] if "default" in prop else "")
        rows.append([name, _type_str(prop), "✓" if name in required else "", desc])
    return rows


def _request_body_ref(request_body: dict) -> str:
    for media in request_body.get("content", {}).values():
        ref = media.get("schema", {}).get("$ref", "")
        if ref:
            return ref.rsplit("/", 1)[-1]
    return ""


def _ref_in_prop(prop: dict) -> str:
    """속성에서 참조하는 스키마 이름을 찾는다($ref / allOf / anyOf / array items)."""
    if not isinstance(prop, dict):
        return ""
    if "$ref" in prop:
        return prop["$ref"].rsplit("/", 1)[-1]
    for key in ("allOf", "anyOf", "oneOf"):
        for sub in prop.get(key, []):
            ref = _ref_in_prop(sub)
            if ref:
                return ref
    if prop.get("type") == "array":
        return _ref_in_prop(prop.get("items", {}))
    return ""


def _success_response_ref(op: dict) -> tuple:
    """성공 응답(2xx)의 스키마 이름 → (status_code, ref). 없으면 (None, "")."""
    responses = op.get("responses", {})
    for code in ("200", "201"):
        resp = responses.get(code)
        if not resp:
            continue
        for media in resp.get("content", {}).values():
            ref = media.get("schema", {}).get("$ref", "")
            if ref:
                return code, ref.rsplit("/", 1)[-1]
    return None, ""


# ---------------------------------------------------------------------------
# 페이지 본문 블록 구성
# ---------------------------------------------------------------------------

def _build_blocks(op: dict, schemas: dict, errors: list | None = None, message: str = "") -> list[dict]:
    blocks = []

    # 설명
    desc = op.get("description") or op.get("summary") or ""
    if desc:
        blocks.append(_para(desc))

    # Query / Path 파라미터
    params = op.get("parameters", [])
    if params:
        blocks.append(_h3("Parameters"))
        rows = [[p.get("name", ""), p.get("in", ""),
                 "✓" if p.get("required") else "",
                 _type_str(p.get("schema", {})),
                 p.get("description", "")] for p in params]
        blocks.append(_table(["이름", "위치", "필수", "타입", "설명"], rows))

    # Request Body
    if op.get("requestBody"):
        ref = _request_body_ref(op["requestBody"])
        blocks.append(_h3("Request Body"))
        if ref and schemas.get(ref, {}).get("properties"):
            blocks.append(_para(f"스키마: {ref}"))
            blocks.append(_table(["필드", "타입", "필수", "설명"], _expand_schema(ref, schemas)))
        elif ref:
            blocks.append(_para(f"스키마: {ref} (multipart/form 또는 단순 본문)"))
        else:
            blocks.append(_para("본문 있음 (스키마 미선언)"))

    # Response — response_model 이 선언돼 있으면 실제 스키마를 펼치고, 없으면 공통 엔벨로프
    blocks.append(_h3("Response"))
    if message:
        blocks.append(_para(f'성공 시 message: "{message}"'))
    status_code, resp_ref = _success_response_ref(op)
    if resp_ref and schemas.get(resp_ref, {}).get("properties"):
        blocks.append(_para(f"성공 응답({status_code}) 스키마: {resp_ref}"))
        blocks.append(_table(["필드", "타입", "필수", "설명"], _expand_schema(resp_ref, schemas)))
        # data 필드가 다른 스키마(페이로드)를 참조하면 한 단계 더 펼친다
        data_prop = schemas.get(resp_ref, {}).get("properties", {}).get("data", {})
        nested = _ref_in_prop(data_prop)
        if nested and schemas.get(nested, {}).get("properties"):
            blocks.append(_para(f"└ data 페이로드 스키마: {nested}"))
            blocks.append(_table(["필드", "타입", "필수", "설명"], _expand_schema(nested, schemas)))
    else:
        blocks.append(_para("모든 응답은 공통 엔벨로프로 감싸집니다. 이 엔드포인트는 response_model 이 "
                            "선언돼 있지 않아 data 페이로드 형태가 자동 추출되지 않습니다 — "
                            "정확한 형태는 /docs(Swagger) 또는 schemas/ 의 {Entity}Response 를 참고하세요."))
        blocks.append(_code('{\n  "code": 200,\n  "message": "성공 메시지",\n  "data": { /* 엔드포인트별 페이로드 */ }\n}'))

    # Errors — 코드에서 추출한 엔드포인트별 실제 메시지(있으면) + 공통 안내
    blocks.append(_h3("Errors"))
    if errors:
        blocks.append(_para('코드에서 추출한 이 엔드포인트의 실제 에러 응답입니다. 동일한 엔벨로프로 '
                            '반환됩니다 (예: {"code": 404, "message": "<메시지>", "data": null}). '
                            "이 외에 422(요청 형식 오류)·500(서버 오류)도 발생할 수 있습니다."))
        rows = sorted([[code, exc, msg] for code, exc, msg in errors], key=lambda r: r[0])
        blocks.append(_table(["HTTP", "예외", "메시지"], rows))
    else:
        blocks.append(_para("이 엔드포인트에 매핑된 커스텀 에러 메시지는 추출되지 않았습니다. "
                            "발생 가능한 공통 에러는 다음과 같습니다(동일 엔벨로프)."))
        blocks.append(_table(["HTTP", "예외", "설명"], [[c[0], c[1], c[2]] for c in COMMON_ERRORS]))

    return blocks


# ---------------------------------------------------------------------------
# OpenAPI 추출
# ---------------------------------------------------------------------------

def extract():
    """app.openapi() → (rows, schemas). 각 row 는 op(raw) 포함."""
    from main import app  # OPENAPI_EXPORT=1 상태에서 안전하게 import

    spec = app.openapi()
    schemas = spec.get("components", {}).get("schemas", {})
    rows = []
    for path, item in sorted(spec.get("paths", {}).items()):
        for method, op in item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            method_u = method.upper()
            tags = op.get("tags") or []
            tag = tags[0] if tags else ""
            params = []
            for p in op.get("parameters", []):
                req = "*" if p.get("required") else ""
                params.append(f"{p.get('name', '')}{req} ({p.get('in', '')})")
            if op.get("requestBody"):
                ref = _request_body_ref(op["requestBody"])
                params.append(f"body: {ref}" if ref else "body")
            rows.append({
                "key": f"{method_u} {path}",
                "method": method_u,
                "path": path,
                "tag": tag,
                "summary": op.get("summary") or op.get("description") or "",
                "params": ", ".join(params),
                "op": op,
            })
    return rows, schemas


# ---------------------------------------------------------------------------
# Notion DB 생성 (--init-db)
# ---------------------------------------------------------------------------

def init_database():
    parent_page = os.getenv("NOTION_PARENT_PAGE_ID")
    if not parent_page:
        sys.exit("ERROR: --init-db 에는 NOTION_PARENT_PAGE_ID 가 필요합니다.")

    payload = {
        "parent": {"type": "page_id", "page_id": parent_page},
        "is_inline": True,
        "title": [{"type": "text", "text": {"content": "API 명세 (자동 동기화)"}}],
        "properties": {
            "Endpoint": {"title": {}},
            "Method": {"select": {"options": [
                {"name": m} for m in ("GET", "POST", "PUT", "PATCH", "DELETE")
            ]}},
            "Tag": {"select": {}},
            "작성자": {"select": {}},
            "Path": {"rich_text": {}},
            "Summary": {"rich_text": {}},
        },
    }
    res = _request("POST", f"{NOTION_API}/databases", json=payload)
    if res.status_code >= 300:
        sys.exit(f"ERROR: DB 생성 실패 [{res.status_code}] {res.text}")
    db_id = res.json()["id"]
    print(f"OK: 데이터베이스 생성 완료\nNOTION_DATABASE_ID={db_id}")
    print("→ 이 값을 GitLab CI/CD Variables 의 NOTION_DATABASE_ID 로 등록하세요.")


# ---------------------------------------------------------------------------
# Notion 동기화 (upsert + 본문 갱신 + archive)
# ---------------------------------------------------------------------------

def _fetch_existing(db_id: str) -> dict[str, str]:
    existing = {}
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = _request("POST", f"{NOTION_API}/databases/{db_id}/query", json=body)
        if res.status_code >= 300:
            sys.exit(f"ERROR: DB 조회 실패 [{res.status_code}] {res.text}")
        data = res.json()
        for page in data["results"]:
            title = page["properties"].get("Endpoint", {}).get("title", [])
            key = title[0]["plain_text"] if title else ""
            if key:
                existing[key] = page["id"]
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return existing


def _row_properties(row: dict, author: str | None = None) -> dict:
    props = {
        "Endpoint": _title(row["key"]),
        "Method": _select(row["method"]),
        "Tag": _select(row["tag"]),
        "Path": _rich_text(row["path"]),
        "Summary": _rich_text(row["summary"]),
    }
    if author is not None:
        props["작성자"] = _select(author)
    return props


def _clear_children(page_id: str):
    """페이지의 기존 본문 블록을 모두 삭제(본문 갱신 전)."""
    cursor = None
    while True:
        url = f"{NOTION_API}/blocks/{page_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        res = _request("GET", url)
        if res.status_code >= 300:
            return  # 조회 실패 시 갱신 생략(best-effort)
        data = res.json()
        for block in data["results"]:
            _request("DELETE", f"{NOTION_API}/blocks/{block['id']}")
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")


def _append_children(page_id: str, blocks: list[dict]):
    for i in range(0, len(blocks), CHILDREN_LIMIT):
        chunk = blocks[i:i + CHILDREN_LIMIT]
        res = _request("PATCH", f"{NOTION_API}/blocks/{page_id}/children", json={"children": chunk})
        if res.status_code >= 300:
            sys.exit(f"ERROR: 본문 추가 실패 [{res.status_code}] {res.text}")


def sync():
    db_id = os.getenv("NOTION_DATABASE_ID")
    if not db_id:
        sys.exit("ERROR: NOTION_DATABASE_ID 환경변수가 필요합니다.")

    from error_index import build_error_index  # 코드 정적 분석 → 엔드포인트별 에러
    from author_index import build_author_index  # git 기록 → 엔드포인트별 원작자
    from message_index import build_message_index  # 라우터 → 엔드포인트별 성공 메시지

    rows, schemas = extract()
    err_index = build_error_index()
    author_index = build_author_index()
    msg_index = build_message_index()
    existing = _fetch_existing(db_id)
    spec_keys = {r["key"] for r in rows}

    created = updated = archived = 0

    for row in rows:
        author = author_index.get((row["method"], row["path"]), "")
        props = _row_properties(row, author=author)
        errors = err_index.get((row["method"], row["path"]))
        message = msg_index.get((row["method"], row["path"]), "")
        blocks = _build_blocks(row["op"], schemas, errors, message)
        page_id = existing.get(row["key"])
        if page_id:
            res = _request("PATCH", f"{NOTION_API}/pages/{page_id}", json={"properties": props})
            if res.status_code >= 300:
                sys.exit(f"ERROR: update 실패 ({row['key']}) [{res.status_code}] {res.text}")
            _clear_children(page_id)
            _append_children(page_id, blocks)
            updated += 1
        else:
            res = _request("POST", f"{NOTION_API}/pages", json={
                "parent": {"database_id": db_id},
                "properties": props,
                "children": blocks[:CHILDREN_LIMIT],
            })
            if res.status_code >= 300:
                sys.exit(f"ERROR: create 실패 ({row['key']}) [{res.status_code}] {res.text}")
            new_id = res.json()["id"]
            if len(blocks) > CHILDREN_LIMIT:
                _append_children(new_id, blocks[CHILDREN_LIMIT:])
            created += 1
        print(f"  · {row['key']}")

    # 코드에서 사라진 엔드포인트 archive
    for key, page_id in existing.items():
        if key not in spec_keys:
            res = _request("PATCH", f"{NOTION_API}/pages/{page_id}", json={"archived": True})
            if res.status_code < 300:
                archived += 1

    print(f"\nOK: created={created}, updated={updated}, archived={archived}, total_spec={len(rows)}")


def migrate_columns():
    """기존 DB 스키마 정리 — Parameters 컬럼 제거 + 작성자 컬럼 추가."""
    db_id = os.getenv("NOTION_DATABASE_ID")
    if not db_id:
        sys.exit("ERROR: NOTION_DATABASE_ID 환경변수가 필요합니다.")
    payload = {"properties": {
        "Parameters": None,        # null → 컬럼 삭제
        "작성자": {"select": {}},  # 없으면 추가, 있으면 유지
    }}
    res = _request("PATCH", f"{NOTION_API}/databases/{db_id}", json=payload)
    if res.status_code >= 300:
        sys.exit(f"ERROR: 컬럼 마이그레이션 실패 [{res.status_code}] {res.text}")
    print("OK: Parameters 컬럼 삭제 + 작성자 컬럼 추가 완료")


def fill_authors():
    """본문은 건드리지 않고 각 페이지의 작성자 속성만 채운다(빠름)."""
    db_id = os.getenv("NOTION_DATABASE_ID")
    if not db_id:
        sys.exit("ERROR: NOTION_DATABASE_ID 환경변수가 필요합니다.")
    from author_index import build_author_index
    authors_by_key = {f"{m} {p}": a for (m, p), a in build_author_index().items()}
    existing = _fetch_existing(db_id)
    filled = 0
    for key, page_id in existing.items():
        author = authors_by_key.get(key, "")
        res = _request("PATCH", f"{NOTION_API}/pages/{page_id}",
                       json={"properties": {"작성자": _select(author)}})
        if res.status_code >= 300:
            sys.exit(f"ERROR: 작성자 갱신 실패 ({key}) [{res.status_code}] {res.text}")
        filled += 1
    print(f"OK: 작성자 채움 {filled}건")


def rebuild():
    """전체 재생성 — 기존 라이브 페이지를 archive 하고 본문 포함해 새로 만든다.

    in-place 본문 갱신(블록 1개씩 삭제)은 페이지당 요청이 많아 느리다. 재생성은
    페이지당 생성 1요청이라 훨씬 빠르고, 강제 종료 등으로 본문 상태가 섞였을 때
    한 번에 일관되게 맞춘다. (옛 페이지는 휴지통으로 가며 30일 후 자동 정리)
    """
    db_id = os.getenv("NOTION_DATABASE_ID")
    if not db_id:
        sys.exit("ERROR: NOTION_DATABASE_ID 환경변수가 필요합니다.")

    from error_index import build_error_index
    from author_index import build_author_index
    from message_index import build_message_index

    rows, schemas = extract()
    err_index = build_error_index()
    author_index = build_author_index()
    msg_index = build_message_index()
    existing = _fetch_existing(db_id)

    # 1) 기존 라이브 페이지 archive
    for key, page_id in existing.items():
        _request("PATCH", f"{NOTION_API}/pages/{page_id}", json={"archived": True})
    print(f"  archived {len(existing)}")

    # 2) 본문 포함해 새로 생성
    created = 0
    for row in rows:
        author = author_index.get((row["method"], row["path"]), "")
        props = _row_properties(row, author=author)
        errors = err_index.get((row["method"], row["path"]))
        message = msg_index.get((row["method"], row["path"]), "")
        blocks = _build_blocks(row["op"], schemas, errors, message)
        res = _request("POST", f"{NOTION_API}/pages", json={
            "parent": {"database_id": db_id},
            "properties": props,
            "children": blocks[:CHILDREN_LIMIT],
        })
        if res.status_code >= 300:
            sys.exit(f"ERROR: create 실패 ({row['key']}) [{res.status_code}] {res.text}")
        new_id = res.json()["id"]
        if len(blocks) > CHILDREN_LIMIT:
            _append_children(new_id, blocks[CHILDREN_LIMIT:])
        created += 1
        print(f"  · {row['key']}")
    print(f"\nOK: rebuild created={created} (archived {len(existing)})")


def dry_run():
    rows, _ = extract()
    for r in rows:
        print(f"[{r['tag'] or '-'}] {r['key']}  | {r['summary']}  | {r['params']}")
    print(f"\ntotal: {len(rows)} endpoints")


def main():
    if "--init-db" in sys.argv:
        init_database()
    elif "--migrate" in sys.argv:
        migrate_columns()
    elif "--authors-only" in sys.argv:
        fill_authors()
    elif "--rebuild" in sys.argv:
        rebuild()
    elif "--dry-run" in sys.argv:
        dry_run()
    else:
        sync()


if __name__ == "__main__":
    main()
