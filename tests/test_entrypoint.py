"""
Smoke test for the production launcher.

Nothing else imports adder/server.py, so a change that broke its imports went
undetected until systemd tried to start the service. Importing it here means
the test suite fails first instead.
"""

import importlib
import os

os.environ.setdefault("API_TOKEN", "test-token")


def test_server_module_imports():
    module = importlib.import_module("adder.server")
    assert hasattr(module, "main")


def test_server_has_host_and_port():
    module = importlib.import_module("adder.server")
    assert isinstance(module.PORT, int)
    assert isinstance(module.HOST, str)
    assert module.HOST, "HOST must not be empty"


def test_entry_point_is_callable():
    module = importlib.import_module("adder.server")
    assert callable(module.main)


def _desktop_tree():
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "desktop" / "local-spotify.py"
    return ast.parse(source.read_text(encoding="utf-8"))


def test_perf_handler_only_with_env():
    """The window's measuring hook exists only under LOCAL_SPOTIFY_PERF=1.

    Read from the source, not imported: the venv and CI have no gi. The hook
    posts timings out of the page; in the everyday window it must not exist.
    """
    import ast

    tree = _desktop_tree()
    perf_flag = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "PERF" for t in node.targets)
    ]
    assert perf_flag, "PERF flag is not defined"
    assert "LOCAL_SPOTIFY_PERF" in ast.unparse(perf_flag[0].value)

    guarded = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "PERF" in ast.unparse(node.test):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and ast.unparse(inner.func).endswith(
                    "register_script_message_handler"
                ):
                    guarded.append(ast.unparse(inner))
    registered = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func).endswith("register_script_message_handler")
        and "perf" in ast.unparse(node)
    ]
    assert registered, "no perf message handler"
    assert all(call in guarded for call in registered), "perf handler outside `if PERF`"
