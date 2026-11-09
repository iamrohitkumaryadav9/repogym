"""Unit tests never touch the network: any TCP/UDP connection attempt fails the test.

Only tests marked ``live`` (manual, never in CI; they call the Gemini API) are exempt.
"""

import socket

import pytest


class NetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _no_network(monkeypatch, request):
    if request.node.get_closest_marker("live"):
        return
    real_connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise NetworkBlocked(f"unit tests must not use the network (tried {address!r})")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket, "create_connection", _refuse)


def _refuse(address, *args, **kwargs):
    raise NetworkBlocked(f"unit tests must not use the network (tried {address!r})")
