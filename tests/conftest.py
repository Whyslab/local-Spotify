"""Suite-wide guard: tests must never reach YouTube, Deezer or iTunes.

The service swallows network errors on purpose (a failed cover lookup must not
fail a download), so simply blocking sockets would hide a test that forgot to
mock something. Instead every attempted connection is recorded and the test
that made it fails at teardown, naming the address.
"""

import socket

import pytest

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def refuse(self, address):
        if self.family == socket.AF_UNIX:
            return _real_connect(self, address)
        attempts.append(address)
        raise ConnectionRefusedError(f"network access in tests is blocked: {address}")

    def refuse_ex(self, address):
        if self.family == socket.AF_UNIX:
            return _real_connect_ex(self, address)
        attempts.append(address)
        return 111  # ECONNREFUSED

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_ex)

    yield

    assert not attempts, (
        f"test tried to open network connections {attempts}; "
        "mock the call (yt-dlp, enrich, requests) instead"
    )
