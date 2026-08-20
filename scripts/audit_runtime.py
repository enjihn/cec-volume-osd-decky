#!/usr/bin/env python3
"""Static release boundary checks for the Python runtime."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_BUSCTL_MEMBERS = {
    "GetNameOwner",
    "GetManagedObjects",
    "GetAudioStatus",
    "Get",
}
FORBIDDEN_IMPORTS = {"cec", "cecctl", "pycec", "subprocess"}


def call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def main() -> None:
    failures: list[str] = []
    for path in [ROOT / "main.py", *(ROOT / "py_modules").glob("*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0].lower() in FORBIDDEN_IMPORTS:
                        failures.append(f"{path}: forbidden import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".", 1)[0].lower() in FORBIDDEN_IMPORTS:
                    failures.append(f"{path}: forbidden import {node.module}")
            elif isinstance(node, ast.Call) and call_name(node) == "_call_command":
                string_args = {
                    value.value
                    for value in node.args
                    if isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                }
                members = string_args & ALLOWED_BUSCTL_MEMBERS
                if len(members) != 1:
                    failures.append(
                        f"{path}:{node.lineno}: busctl call member is not exactly allowlisted"
                    )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("/dev/cec"):
                    failures.append(
                        f"{path}:{node.lineno}: kernel CEC device path is forbidden"
                    )

    if failures:
        raise SystemExit("\n".join(failures))


if __name__ == "__main__":
    main()
