"""Reads do not register a pre_tool_call hook and do not ask.

A seen-uid write is cron bookkeeping. There is no approve line.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _hook_calls(path: Path) -> list[tuple]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "register_hook":
                args = []
                for arg in node.args:
                    if isinstance(arg, ast.Constant):
                        args.append(arg.value)
                    elif isinstance(arg, ast.Name):
                        args.append(arg.id)
                found.append(tuple(args))
    return found


def test_register_adds_no_hook():
    assert _hook_calls(ROOT / "__init__.py") == []
    assert _hook_calls(ROOT / "service.py") == []


def test_no_approve_helper_and_cron_mark_is_public():
    text = (ROOT / "service.py").read_text(encoding="utf-8")
    assert "def on_pre_tool_call" not in text
    assert "register_hook" not in text
    assert "current_cron_execution" in text
    assert "approval_context" not in text
    assert "tools.approval" not in text
