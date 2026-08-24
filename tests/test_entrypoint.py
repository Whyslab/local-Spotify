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
