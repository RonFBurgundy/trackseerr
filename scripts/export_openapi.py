#!/usr/bin/env python3
"""Export the FastAPI OpenAPI schema as deterministic JSON (stdout, or the path given as argv[1]).

Side-effect free: builds the app via ``create_app()`` with no Database/Config (no workers, no
servers, no network), pins ROLE to all-in-one and points DATA_DIR/CONFIG_DIR at a throwaway temp dir.
Run from the repo root with PYTHONPATH set to the repo root.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile


def _dedupe_operation_ids(schema: dict) -> dict:
    """Make operationIds unique so ``openapi-typescript`` does not emit duplicate keys (TS2300).

    ``api_route(..., methods=["GET", "HEAD"])`` gives both methods one id (e.g. ``health_ready_api_health_ready_get``).
    The first occurrence in sorted path/method order keeps its id; later ones get a ``__<method>`` suffix.
    """
    seen: set[str] = set()
    for path in sorted(schema.get("paths", {})):
        ops = schema["paths"][path]
        for method in sorted(ops):
            op = ops[method]
            op_id = op.get("operationId") if isinstance(op, dict) else None
            if not op_id:
                continue
            if op_id in seen:
                op["operationId"] = f"{op_id}__{method}"
            seen.add(op["operationId"])
    return schema


def main(argv: list[str]) -> int:
    with tempfile.TemporaryDirectory(prefix="openapi-export-") as tmp:
        os.environ["DATA_DIR"] = tmp
        os.environ["CONFIG_DIR"] = tmp
        os.environ["ROLE"] = "all-in-one"
        os.environ.pop("ENABLE_API_DOCS", None)
        from trackseerr.api.app import create_app

        schema = _dedupe_operation_ids(create_app().openapi())
    text = json.dumps(schema, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    if len(argv) > 1:
        with open(argv[1], "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
