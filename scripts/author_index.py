# -*- coding: utf-8 -*-
"""각 엔드포인트의 원작자(최초 작성자)를 git 기록에서 추출한다.

라우터 함수의 라인 범위를 AST 로 구한 뒤, `git log -L<start>,<end>:<file>` 로 그
라인들의 변경 이력을 따라가 가장 오래된(=최초) 커밋의 작성자를 원작자로 본다.

build_author_index() → { (METHOD, path): author_name }

주의: 정확한 원작자 추적에는 전체 git 히스토리가 필요하다. CI 처럼 shallow clone
(GIT_DEPTH 제한) 환경에서는 가장 오래된 '보이는' 커밋의 작성자가 잡힐 수 있다 →
.gitlab-ci.yml 에서 GIT_DEPTH: 0 로 전체 히스토리를 받는다.
"""
import ast
import glob
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTERS_DIR = os.path.join(ROOT, "routers")
HTTP_DECOS = {"get", "post", "put", "patch", "delete"}


def _router_prefix(tree) -> str:
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "APIRouter":
            for kw in n.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    return kw.value.value
    return ""


def _orig_author(rel_path: str, start: int, end: int) -> str:
    """라인 범위의 변경 이력 중 가장 오래된 커밋의 작성자."""
    try:
        out = subprocess.run(
            ["git", "log", f"-L{start},{end}:{rel_path}", "-s", "--format=%x00%an"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
    except Exception:
        return ""
    if out.returncode != 0:
        return ""
    # -s 로 diff 는 억제되지만 안전하게 NUL 마커가 붙은 줄만 작성자로 취급
    authors = [ln[1:].strip() for ln in out.stdout.splitlines() if ln.startswith("\x00")]
    return authors[-1] if authors else ""


def build_author_index() -> dict:
    result = {}
    for path in glob.glob(os.path.join(ROUTERS_DIR, "*.py")):
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        prefix = _router_prefix(tree)
        for n in tree.body:
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decos = [d for d in n.decorator_list
                     if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                     and d.func.attr in HTTP_DECOS]
            if not decos:
                continue
            start = n.decorator_list[0].lineno
            end = n.end_lineno or start
            author = _orig_author(rel, start, end)
            for d in decos:
                sub = d.args[0].value if (d.args and isinstance(d.args[0], ast.Constant)) else ""
                result[(d.func.attr.upper(), prefix + sub)] = author
    return result


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    idx = build_author_index()
    for (m, p), a in sorted(idx.items()):
        print(f"{a or '(미상)':14} {m:6} {p}")
    print(f"\n총 {len(idx)} 엔드포인트, 작성자 추출 {sum(1 for v in idx.values() if v)}")
