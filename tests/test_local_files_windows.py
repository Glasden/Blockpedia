"""Windows requires a writable handle for FlushFileBuffers."""

import os

import pytest

from blockpedia.local_files import sync_tree


@pytest.mark.skipif(os.name != "nt", reason="Windows fsync behavior")
def test_sync_tree_flushes_files_on_windows(tmp_path):
    file = tmp_path / "manifest.json"
    file.write_bytes(b"{}")

    sync_tree(tmp_path)

    assert file.read_bytes() == b"{}"
