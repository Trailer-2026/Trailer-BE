# -*- coding: utf-8 -*-
"""라우터 → 서비스 호출을 정적 분석해 엔드포인트별 에러 응답을 추출한다.

OpenAPI 스펙에는 커스텀 에러(404/400/409/401)가 선언돼 있지 않으므로, 코드를
AST 로 파싱해 각 엔드포인트가 던질 수 있는 실제 예외 메시지를 수집한다.

수집 패턴 (전부 문자열 리터럴/ f-string):
  - raise NotFoundException("..")   / BadRequestException / DuplicateException
    / UnauthorizedException
  - ensure_found(<entity>, "..")  → 404

추적: 라우터 엔드포인트 함수 → 호출하는 서비스 함수(들) → 그 함수가 다시 호출하는
서비스/로컬 함수(전이적 폐쇄, 방문 집합으로 순환 방지).

build_error_index() → { (METHOD, path): [(http_code, exception, message), ...] }
"""
import ast
import os
import glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES_DIR = os.path.join(ROOT, "services")
ROUTERS_DIR = os.path.join(ROOT, "routers")

EXC_CODE = {
    "NotFoundException": "404",
    "BadRequestException": "400",
    "DuplicateException": "409",
    "UnauthorizedException": "401",
}
HTTP_DECOS = {"get", "post", "put", "patch", "delete"}


def _message(node) -> str:
    """Constant 문자열 또는 f-string → 메시지 텍스트. 동적이면 표시용 문자열."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                try:
                    parts.append("{" + ast.unparse(v.value) + "}")
                except Exception:
                    parts.append("{…}")
        return "".join(parts)
    return "(동적 메시지)"


def _service_aliases(tree) -> dict:
    """모듈 내 `from services import X` 별칭 → 서비스 모듈 stem."""
    aliases = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "services":
            for a in n.names:
                aliases[a.asname or a.name] = a.name
    return aliases


def _analyze_func(fn, modstem: str, aliases: dict) -> dict:
    """함수 1개 → {errors: [(code,exc,msg)], calls: [(modstem, funcname)]}."""
    errors, calls = [], []
    for node in ast.walk(fn):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            f = node.exc.func
            if isinstance(f, ast.Name) and f.id in EXC_CODE:
                msg = _message(node.exc.args[0]) if node.exc.args else ""
                errors.append((EXC_CODE[f.id], f.id, msg))
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == "ensure_found":
                msg = _message(node.args[1]) if len(node.args) > 1 else ""
                errors.append(("404", "NotFoundException", msg))
            elif isinstance(f, ast.Name):
                calls.append((modstem, f.id))  # 같은 모듈 로컬 호출
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                if f.value.id in aliases:
                    calls.append((aliases[f.value.id], f.attr))  # 서비스 모듈 호출
    return {"errors": errors, "calls": calls}


def _build_service_index() -> dict:
    """{modstem: {funcname: {errors, calls}}} — 모든 서비스 모듈."""
    index = {}
    for path in glob.glob(os.path.join(SERVICES_DIR, "*.py")):
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        modstem = os.path.splitext(os.path.basename(path))[0]
        aliases = _service_aliases(tree)
        funcs = {}
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs[n.name] = _analyze_func(n, modstem, aliases)
        index[modstem] = funcs
    return index


def _collect(info: dict, svc_index: dict) -> list:
    """함수 info → 전이적 에러 목록(중복 제거, 순서 유지)."""
    errs = list(info["errors"])
    visited, stack = set(), list(info["calls"])
    while stack:
        mod, func = stack.pop()
        if (mod, func) in visited:
            continue
        visited.add((mod, func))
        fn = svc_index.get(mod, {}).get(func)
        if not fn:
            continue
        errs.extend(fn["errors"])
        stack.extend(fn["calls"])
    seen, out = set(), []
    for code, exc, msg in errs:
        key = (code, msg)
        if key not in seen:
            seen.add(key)
            out.append((code, exc, msg))
    return out


def _router_prefix(tree) -> str:
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "APIRouter":
            for kw in n.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    return kw.value.value
    return ""


def build_error_index() -> dict:
    svc_index = _build_service_index()
    result = {}
    for path in glob.glob(os.path.join(ROUTERS_DIR, "*.py")):
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        prefix = _router_prefix(tree)
        aliases = _service_aliases(tree)
        modstem = os.path.splitext(os.path.basename(path))[0]
        for n in tree.body:
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in n.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                method = dec.func.attr
                if method not in HTTP_DECOS:
                    continue
                sub = dec.args[0].value if (dec.args and isinstance(dec.args[0], ast.Constant)) else ""
                full = prefix + sub
                info = _analyze_func(n, modstem, aliases)
                errs = _collect(info, svc_index)
                if errs:
                    result[(method.upper(), full)] = errs
    return result


if __name__ == "__main__":
    import sys
    idx = build_error_index()
    sys.stdout.reconfigure(encoding="utf-8")
    for (m, p), errs in sorted(idx.items()):
        print(f"\n{m} {p}")
        for code, exc, msg in errs:
            print(f"  {code} {exc}: {msg}")
    print(f"\n총 {len(idx)} 엔드포인트에 에러 추출됨")
