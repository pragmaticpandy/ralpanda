"""Lint: ensure subprocess calls never leak output to the terminal (corrupts curses TUI)."""

import ast
import unittest
from pathlib import Path


# Modules that run under the curses TUI — their subprocess calls must never
# write to the real terminal.
_TUI_MODULES = ("git", "agent")


class TestNoLeakedSubprocessOutput(unittest.TestCase):
    """Every subprocess call in TUI-hosted modules must capture/redirect output."""

    def test_subprocess_run_captures_output(self):
        """subprocess.run() must use capture_output=True or redirect stdout."""
        violations = []
        for mod_name in _TUI_MODULES:
            for node in _find_calls(mod_name, "run"):
                if not _call_captures_output(node):
                    violations.append(
                        f"{mod_name}.py:{node.lineno}: subprocess.run() missing output capture"
                    )
        self.assertEqual(violations, [], "\n".join(violations))

    def test_subprocess_popen_captures_output(self):
        """subprocess.Popen() must redirect stdout."""
        violations = []
        for mod_name in _TUI_MODULES:
            for node in _find_calls(mod_name, "Popen"):
                if not _call_has_kwarg(node, "stdout"):
                    violations.append(
                        f"{mod_name}.py:{node.lineno}: subprocess.Popen() missing stdout redirect"
                    )
        self.assertEqual(violations, [], "\n".join(violations))


def _find_calls(mod_name: str, method: str):
    """Yield all ast.Call nodes for subprocess.<method>() in the given module."""
    pkg_dir = Path(__file__).resolve().parent
    src = (pkg_dir / f"{mod_name}.py").read_text()
    tree = ast.parse(src, filename=f"{mod_name}.py")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == method
        ):
            yield node


def _call_captures_output(node) -> bool:
    kw_names = {kw.arg for kw in node.keywords if kw.arg is not None}
    return "capture_output" in kw_names or "stdout" in kw_names


def _call_has_kwarg(node, name: str) -> bool:
    return any(kw.arg == name for kw in node.keywords)


if __name__ == "__main__":
    unittest.main()
