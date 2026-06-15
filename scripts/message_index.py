# -*- coding: utf-8 -*-
"""라우터 엔드포인트의 성공 응답 메시지를 정적 분석해 추출한다.

응답 메시지는 CommonResponse.success_response("<메시지>", ...) 의 첫 인자 리터럴로
런타임에 전달되어 OpenAPI 스키마에는 담기지 않는다. 라우터 핸들러 본문을 AST 로
파싱해 엔드포인트별 성공 메시지를 모은다(에러 메시지 추출과 동일한 방식).

build_message_index() → { (METHOD, path): "성공 메시지" }
"""
import ast
import os
import glob

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


def _success_message(fn) -> str:
    """함수 본문에서 success_response("<리터럴>", ...) 의 첫 문자열 인자를 찾는다."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "success_response" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    return arg.value
    return ""


def build_message_index() -> dict:
    result = {}
    for path in glob.glob(os.path.join(ROUTERS_DIR, "*.py")):
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        prefix = _router_prefix(tree)
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
                msg = _success_message(n)
                if msg:
                    result[(method.upper(), full)] = msg
    return result


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    for (m, p), msg in sorted(build_message_index().items()):
        print(f"{m} {p}  ->  {msg}")
