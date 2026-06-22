# -*- coding: utf-8 -*-
"""라우터 → 서비스/유틸/코어 호출을 정적 분석해 엔드포인트별 에러 응답을 추출한다.

OpenAPI 스펙에는 커스텀 에러(404/400/409/401)가 선언돼 있지 않으므로, 코드를
AST 로 파싱해 각 엔드포인트가 던질 수 있는 실제 예외 메시지를 수집한다.

수집 패턴 (전부 문자열 리터럴/ f-string):
  - raise NotFoundException("..")   / BadRequestException / DuplicateException
    / UnauthorizedException
  - ensure_found(<entity>, "..")  → 404

추적 범위:
  라우터 엔드포인트 함수 → 호출하는 함수(들)를 전이적으로 따라간다.
  - services / utils / core / databases.daos 모듈을 모두 스캔 대상으로 한다.
  - `from pkg import module`(모듈 alias)과 `from pkg.mod import func`(함수 import)을
    구분해 호출 대상을 해석한다.
  - 엔드포인트 시그니처의 `Depends(func)` 의존성도 호출로 간주해(예: get_current_user)
    그 안의 예외까지 수집한다.
  - 방문 집합으로 순환을 방지한다.

build_error_index() → { (METHOD, path): [(http_code, exception, message), ...] }
"""
import ast
import os
import glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTERS_DIR = os.path.join(ROOT, "routers")
# 호출 추적 대상으로 스캔할 디렉토리 (라우터에서 전이적으로 도달 가능한 코드)
SCAN_DIRS = [
    os.path.join(ROOT, "services"),
    os.path.join(ROOT, "utils"),
    os.path.join(ROOT, "core"),
    os.path.join(ROOT, "databases", "daos"),
]

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


def _import_maps(tree, module_stems: set) -> tuple:
    """파일의 import 를 분석해 (module_aliases, func_imports) 반환.

    - module_aliases: 별칭 → 모듈 stem  (예: oauth.foo() / user_dao.create())
    - func_imports:   함수명 → (모듈 stem, 원본 함수명)  (예: decode_token())
    스캔 대상(module_stems)에 속한 것만 담는다(외부 라이브러리는 무시).
    """
    module_aliases, func_imports = {}, {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            last = n.module.split(".")[-1]
            for a in n.names:
                local = a.asname or a.name
                if a.name in module_stems:
                    # from pkg import <module>
                    module_aliases[local] = a.name
                elif last in module_stems:
                    # from pkg.<module> import <func>
                    func_imports[local] = (last, a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                stem = a.name.split(".")[-1]
                if stem in module_stems:
                    module_aliases[a.asname or stem] = stem
    return module_aliases, func_imports


def _resolve_call(node, modstem, module_aliases, func_imports):
    """Call 노드 → 추적할 (모듈, 함수) 또는 None. Depends(func) 도 해석한다."""
    f = node.func
    if isinstance(f, ast.Name):
        if f.id == "Depends" and node.args:
            # 의존성 주입: Depends(get_current_user) → 그 함수를 호출 대상으로
            arg = node.args[0]
            if isinstance(arg, ast.Name):
                return func_imports.get(arg.id, (modstem, arg.id))
            if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name) and arg.value.id in module_aliases:
                return (module_aliases[arg.value.id], arg.attr)
            return None
        # 직접 호출: 다른 모듈에서 import 한 함수면 그 모듈로, 아니면 로컬
        return func_imports.get(f.id, (modstem, f.id))
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        if f.value.id in module_aliases:
            return (module_aliases[f.value.id], f.attr)  # 모듈.함수()
    return None


def _analyze_func(fn, modstem, module_aliases, func_imports) -> dict:
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
                continue
            target = _resolve_call(node, modstem, module_aliases, func_imports)
            if target:
                calls.append(target)
    return {"errors": errors, "calls": calls}


def _module_stems() -> set:
    """스캔 대상 디렉토리의 모든 모듈 stem 집합."""
    stems = set()
    for d in SCAN_DIRS:
        for path in glob.glob(os.path.join(d, "*.py")):
            stems.add(os.path.splitext(os.path.basename(path))[0])
    return stems


def _build_func_index(module_stems: set) -> dict:
    """{modstem: {funcname: {errors, calls}}} — 스캔 대상 전체."""
    index = {}
    for d in SCAN_DIRS:
        for path in glob.glob(os.path.join(d, "*.py")):
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            modstem = os.path.splitext(os.path.basename(path))[0]
            module_aliases, func_imports = _import_maps(tree, module_stems)
            funcs = {}
            for n in tree.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    funcs[n.name] = _analyze_func(n, modstem, module_aliases, func_imports)
            index[modstem] = funcs
    return index


def _collect(info: dict, func_index: dict) -> list:
    """함수 info → 전이적 에러 목록(중복 제거, 순서 유지)."""
    errs = list(info["errors"])
    visited, stack = set(), list(info["calls"])
    while stack:
        mod, func = stack.pop()
        if (mod, func) in visited:
            continue
        visited.add((mod, func))
        fn = func_index.get(mod, {}).get(func)
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
    module_stems = _module_stems()
    func_index = _build_func_index(module_stems)
    result = {}
    for path in glob.glob(os.path.join(ROUTERS_DIR, "*.py")):
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        prefix = _router_prefix(tree)
        module_aliases, func_imports = _import_maps(tree, module_stems)
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
                info = _analyze_func(n, modstem, module_aliases, func_imports)
                errs = _collect(info, func_index)
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
