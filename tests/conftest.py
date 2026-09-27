"""Keep tests independent of a desktop credential service."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_host_keyring(monkeypatch):
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
