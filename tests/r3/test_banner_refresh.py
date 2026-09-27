"""Incomplete legacy refresh records must block new workspace writes."""

import pytest
from blockpedia.services import R3Error
from .test_release_builder import _ready


def test_incomplete_legacy_refresh_blocks_new_workspace_writes(tmp_path):
    service, run_id = _ready(tmp_path)
    try:
        workspace = service.data_root.workspace_dir('26.2', run_id)
        (workspace / 'banner-refresh.v1.json').write_text('{}')
        with pytest.raises(R3Error) as failure:
            service.build_candidate_release(run_id, '26.2', 'build_' + 'a' * 32)
        assert failure.value.code == 'LEGACY_REFRESH_RECOVERY_REQUIRED'
    finally:
        service.close()
