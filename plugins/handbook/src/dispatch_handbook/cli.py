from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .demo import build_demo
from .service import ACTIONS, handle


def absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise argparse.ArgumentTypeError("path must be absolute and cannot contain traversal")
    if path.is_symlink():
        raise argparse.ArgumentTypeError("path cannot be a symlink")
    return path.resolve(strict=False)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="dispatch-handbook")
    subcommands = value.add_subparsers(dest="command", required=True)
    demo = subcommands.add_parser("demo-init", help="explicitly build a local index from declared synthetic data")
    demo.add_argument("--fixture", required=True, type=Path)
    demo.add_argument("--target", required=True, type=absolute_path)
    query = subcommands.add_parser("query", help="run a read-only query against a local index")
    query.add_argument("--index", required=True, type=absolute_path)
    query.add_argument("--action", required=True, choices=sorted(ACTIONS))
    query.add_argument("--question")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "demo-init":
        try:
            receipt = build_demo(args.fixture.resolve(), args.target)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)[:256]}, sort_keys=True))
            return 1
        print(json.dumps({"ok": True, "synthetic": True, "receipt": receipt}, sort_keys=True))
        return 0

    request = {"action": args.action}
    if args.question is not None:
        request["question"] = args.question
    result = handle(request, index_path=args.index)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
