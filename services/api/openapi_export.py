"""Export the API's OpenAPI schema to ``web/openapi.json`` (issue #46; D12).

``python -m services.api.openapi_export [--out PATH|-]``

The schema comes from ``create_app().openapi()`` in-process: no server, no
socket, no database, no lifespan. Output is deterministic (sorted keys, two
space indent, UTF-8, LF, one trailing newline) so it can be committed and
compared byte for byte. Paths are the API's own (``/healthz``); nginx strips
``/api/`` before proxying (architecture.md 11.3).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from services.api.main import create_app

DEFAULT_OUT = Path(__file__).resolve().parents[2] / "web" / "openapi.json"


def build_schema() -> dict[str, Any]:
    schema: dict[str, Any] = create_app().openapi()
    return schema


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write_atomic(target: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m services.api.openapi_export")
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="output file, or - for stdout (default: web/openapi.json)",
    )
    args = parser.parse_args(argv)
    data = render(build_schema()).encode("utf-8")

    if args.out == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return 0

    target = Path(args.out)
    if not target.parent.is_dir():
        print(f"error: output directory does not exist: {target.parent} ({target})", file=sys.stderr)
        return 1
    try:
        _write_atomic(target, data)
    except OSError as exc:
        print(f"error: cannot write {target}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
