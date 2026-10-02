"""scripts/security_check.py: how it reads the machine, without touching it."""

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "security_check.py"
spec = importlib.util.spec_from_file_location("security_check", SCRIPT)
assert spec and spec.loader
sc = importlib.util.module_from_spec(spec)
sys.modules["security_check"] = sc  # dataclasses look the module up by name
spec.loader.exec_module(sc)

SS = """LISTEN 0 2048 0.0.0.0:8787 0.0.0.0:*
LISTEN 0 4096 *:4533 *:*
LISTEN 0 4096 100.96.179.45:443 0.0.0.0:*
LISTEN 0 128 127.0.0.1:5984 0.0.0.0:*
"""


def test_ports_open_beyond_this_machine_point_at_ufw():
    found = {f.check: f for f in sc.check_ports(SS)}
    assert found["service_port"].state == sc.MANUAL
    assert "ufw" in found["service_port"].detail
    assert found["navidrome_port"].state == sc.MANUAL
    loopback = "LISTEN 0 1 127.0.0.1:8787 0.0.0.0:*\nLISTEN 0 1 127.0.0.1:4533 0.0.0.0:*\n"
    assert {f.state for f in sc.check_ports(loopback)} == {sc.OK}
    assert sc.check_ports("")[0].state == sc.FAIL


def test_tailscale_funnel_is_a_failure_and_tailnet_only_is_fine():
    tailnet = "https://arch-laptop.example.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:3100"
    assert sc.check_tailscale(tailnet, tailnet).state == sc.OK
    funnel = "# Funnel on:\n#     - https://arch-laptop.example.ts.net\n"
    assert sc.check_tailscale(funnel, funnel).state == sc.FAIL


def test_secret_files_must_be_private(tmp_path):
    env = tmp_path / ".env"
    env.write_text("API_TOKEN=x")
    env.chmod(0o600)
    assert sc.check_secret_files([env])[0].state == sc.OK
    env.chmod(0o644)
    assert sc.check_secret_files([env])[0].state == sc.FAIL


def test_security_check_flags_default_navidrome_password(monkeypatch):
    monkeypatch.setattr(sc, "subsonic_ping", lambda base, user, password: True)
    # Accepted by the user: reported, not a failure.
    assert sc.check_navidrome_password("http://nd").state == sc.ACCEPT
    monkeypatch.setattr(sc, "ACCEPTED", {})
    assert sc.check_navidrome_password("http://nd").state == sc.FAIL
    monkeypatch.setattr(sc, "subsonic_ping", lambda base, user, password: False)
    assert sc.check_navidrome_password("http://nd").state == sc.OK


def test_the_subsonic_token_is_md5_of_password_and_salt(monkeypatch):
    seen = {}

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *a):
            return b'{"subsonic-response": {"status": "ok"}}'

    def fake_urlopen(url, timeout):
        seen["url"] = url
        return Answer()

    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
    assert sc.subsonic_ping("http://nd", "admin", "admin") is True
    import hashlib

    assert f"t={hashlib.md5(b'adminlsSecCheck').hexdigest()}" in seen["url"]


def test_health_without_a_token_must_say_only_status(monkeypatch):
    def answer(body):
        class A:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, *a):
                return body

        return lambda url, timeout: A()

    monkeypatch.setattr(sc.urllib.request, "urlopen", answer(b'{"status": "healthy"}'))
    assert sc.check_health_without_token("http://svc").state == sc.OK
    leaky = b'{"status": "healthy", "library_path": "/home/x"}'
    monkeypatch.setattr(sc.urllib.request, "urlopen", answer(leaky))
    assert sc.check_health_without_token("http://svc").state == sc.FAIL
