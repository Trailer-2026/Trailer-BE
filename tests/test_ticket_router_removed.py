"""Ticket API 미등록 자체 점검 — `python tests/test_ticket_router_removed.py`."""
import os
import sys
from pathlib import Path

os.environ.setdefault("OPENAPI_EXPORT", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import app


def main() -> None:
    ticket_paths = {
        route.path
        for route in app.routes
        if route.path == "/api/tickets" or route.path.startswith("/api/tickets/")
    }
    assert not ticket_paths, f"사용하지 않는 Ticket API가 등록됨: {sorted(ticket_paths)}"
    print("OK: Ticket API 미등록 자체 점검 통과")


if __name__ == "__main__":
    main()
