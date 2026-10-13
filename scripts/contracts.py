"""API contracts: export them from the app, regenerate typed clients, fail CI on drift.

    uv run python scripts/contracts.py export     # contracts/ <- the app
    uv run python scripts/contracts.py generate   # clients    <- contracts/
    uv run python scripts/contracts.py check      # CI: exit 1 on any drift
    uv run python scripts/contracts.py check --base-url http://localhost:8010

``check`` fails when
  1. contracts/ differs from what the app serves. By default the app is built in-process (same
     code as ``make api``); with --base-url the running app's /openapi.json and GraphQL
     introspection are compared instead, so a deployed instance can be checked too;
  2. any generated client differs from what the pinned generators produce from contracts/.
     Generators run in a temp mirror of their inputs, so the check never touches the tree.
"""

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import httpx
from graphql import (
    build_client_schema,
    build_schema,
    get_introspection_query,
    lexicographic_sort_schema,
    print_schema,
)

from manifest_api.contracts import graphql_sdl, openapi_json

ROOT = Path(__file__).resolve().parents[1]
CODEGEN = ROOT / "tools" / "codegen"

CONTRACTS = Path("contracts")
OPENAPI = CONTRACTS / "openapi.json"
SDL = CONTRACTS / "schema.graphql"

PYTHON_SDK = Path("clients/python")
GRAPHQL_CLIENT = Path("clients/graphql")
GRAPHQL_PACKAGE = GRAPHQL_CLIENT / "src" / "manifest_review_client"
REVIEW_UI = Path("web/review")
TS_TYPES = REVIEW_UI / "src" / "generated" / "rest.d.ts"

GENERATED = (PYTHON_SDK, GRAPHQL_PACKAGE, TS_TYPES)
IGNORED_PARTS = {"__pycache__", ".ruff_cache"}
MAX_DIFF_LINES = 60


# ---------------------------------------------------------------- generation


def _codegen(*args: str, cwd: Path) -> None:
    # The codegen toolchain has its own lockfile; drop the workspace venv uv run exported.
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    command = ["uv", "run", "--project", str(CODEGEN), "--locked", "--quiet", *args]
    subprocess.run(command, cwd=cwd, env=env, check=True, stdout=subprocess.DEVNULL)


def _python_sdk(root: Path) -> None:
    _codegen(
        "openapi-python-client",
        "generate",
        "--path",
        str(root / OPENAPI),
        "--output-path",
        str(root / PYTHON_SDK),
        "--config",
        str(CODEGEN / "openapi-python-client.yaml"),
        "--meta",
        "uv",
        "--overwrite",
        "--fail-on-warning",
        cwd=root,
    )
    shutil.rmtree(root / PYTHON_SDK / ".ruff_cache", ignore_errors=True)


def _graphql_client(root: Path) -> None:
    shutil.rmtree(root / GRAPHQL_PACKAGE, ignore_errors=True)  # drop modules of removed operations
    (root / GRAPHQL_PACKAGE).parent.mkdir(parents=True, exist_ok=True)
    _codegen("ariadne-codegen", "--config", "pyproject.toml", "client", cwd=root / GRAPHQL_CLIENT)


def _typescript(root: Path) -> None:
    binary = ROOT / REVIEW_UI / "node_modules" / ".bin" / "openapi-typescript"
    if not binary.exists():
        subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=ROOT / REVIEW_UI, check=True)
    (root / TS_TYPES).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(binary), str(root / OPENAPI), "--output", str(root / TS_TYPES)],
        cwd=root,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def generate(root: Path) -> None:
    _python_sdk(root)
    _graphql_client(root)
    _typescript(root)


# ---------------------------------------------------------------- comparison


def _snapshot(path: Path) -> dict[str, bytes]:
    """File contents keyed by path relative to ``path`` ("" when ``path`` is a file)."""
    if path.is_file():
        return {"": path.read_bytes()}
    if not path.is_dir():
        return {}
    return {
        file.relative_to(path).as_posix(): file.read_bytes()
        for file in sorted(path.rglob("*"))
        if file.is_file() and not IGNORED_PARTS.intersection(file.relative_to(path).parts)
    }


def _diff(label: str, expected: str, committed: str) -> list[str]:
    lines = list(
        difflib.unified_diff(
            committed.splitlines(),
            expected.splitlines(),
            fromfile=f"committed/{label}",
            tofile=f"expected/{label}",
            lineterm="",
        )
    )
    if len(lines) > MAX_DIFF_LINES:
        lines = [*lines[:MAX_DIFF_LINES], f"... {len(lines) - MAX_DIFF_LINES} more diff lines"]
    return lines


def _compare(root: Path, expected: dict[str, bytes], committed: dict[str, bytes]) -> list[str]:
    report = []
    for name in sorted(expected.keys() | committed.keys()):
        label = (root / name).as_posix() if name else root.as_posix()
        if name not in committed:
            report.append(f"missing: {label}")
        elif name not in expected:
            report.append(f"stale: {label}")
        elif expected[name] != committed[name]:
            report += _diff(label, expected[name].decode(), committed[name].decode())
    return report


# ---------------------------------------------------------------- contract sources


def _in_process_report() -> list[str]:
    expected = {OPENAPI.as_posix(): openapi_json().encode(), SDL.as_posix(): graphql_sdl().encode()}
    committed = {name: (ROOT / name).read_bytes() for name in expected if (ROOT / name).is_file()}
    return _compare(Path(), expected, committed)


def _normalised_sdl(sdl: str) -> str:
    return print_schema(lexicographic_sort_schema(build_schema(sdl)))


def _live_report(base_url: str) -> list[str]:
    """Semantic comparison: a running app may serialise differently but must mean the same."""
    with httpx.Client(base_url=base_url, timeout=10) as client:
        served_openapi = client.get("/openapi.json").raise_for_status().json()
        # Ask for every schema detail SDL can express, or the comparison misses (say) @oneOf.
        query = get_introspection_query(
            descriptions=True,
            specified_by_url=True,
            directive_is_repeatable=True,
            input_value_deprecation=True,
            one_of=True,
        )
        introspection = client.post("/graphql", json={"query": query}).raise_for_status().json()

    report = []
    committed_openapi = json.loads((ROOT / OPENAPI).read_text())
    if served_openapi != committed_openapi:
        report += _diff(
            OPENAPI.as_posix(),
            json.dumps(served_openapi, indent=2, sort_keys=True),
            json.dumps(committed_openapi, indent=2, sort_keys=True),
        )
    served_sdl = print_schema(lexicographic_sort_schema(build_client_schema(introspection["data"])))
    committed_sdl = _normalised_sdl((ROOT / SDL).read_text())
    if served_sdl != committed_sdl:
        report += _diff(SDL.as_posix(), served_sdl, committed_sdl)
    return report


# ---------------------------------------------------------------- commands


def export() -> None:
    (ROOT / CONTRACTS).mkdir(exist_ok=True)
    (ROOT / OPENAPI).write_text(openapi_json())
    (ROOT / SDL).write_text(graphql_sdl())
    print(f"wrote {OPENAPI} and {SDL}")


def check(base_url: str | None) -> int:
    source = f"running app at {base_url}" if base_url else "app (in-process)"
    contract_report = _live_report(base_url) if base_url else _in_process_report()
    _print_section(f"contracts/ vs {source}", contract_report)

    with tempfile.TemporaryDirectory(prefix="manifest-contracts-") as tmp:
        mirror = Path(tmp)
        shutil.copytree(ROOT / CONTRACTS, mirror / CONTRACTS)
        shutil.copytree(
            ROOT / GRAPHQL_CLIENT,
            mirror / GRAPHQL_CLIENT,
            ignore=shutil.ignore_patterns("src", *IGNORED_PARTS),
        )
        generate(mirror)
        client_report = []
        for path in GENERATED:
            client_report += _compare(path, _snapshot(mirror / path), _snapshot(ROOT / path))
    _print_section("generated clients vs contracts/", client_report)

    if contract_report or client_report:
        print("\nContracts drifted. Run `make contracts` and commit the result.", file=sys.stderr)
        return 1
    return 0


def _print_section(title: str, report: list[str]) -> None:
    print(f"{title}: {'DRIFT' if report else 'ok'}")
    for line in report:
        print(f"  {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("export", help="write contracts/ from the app")
    commands.add_parser("generate", help="regenerate every typed client from contracts/")
    check_parser = commands.add_parser("check", help="exit 1 if contracts or clients drifted")
    check_parser.add_argument("--base-url", help="compare against a running app instead")
    args = parser.parse_args(argv)

    actions: dict[str, Callable[[], int | None]] = {
        "export": export,
        "generate": lambda: generate(ROOT),
        "check": lambda: check(args.base_url),
    }
    return actions[args.command]() or 0


if __name__ == "__main__":
    sys.exit(main())
