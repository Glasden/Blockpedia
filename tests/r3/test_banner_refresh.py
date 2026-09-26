"""The retired repair flow must not remove the ordinary banner renderer."""
from pathlib import Path
import importlib.util

import pytest
from blockpedia.services import R3Error
from .test_release_builder import _ready


def test_banner_repair_is_retired_but_common_render_fix_remains():
    root = Path(__file__).parents[2]
    assert importlib.util.find_spec('blockpedia.banner_refresh') is None
    java = root / 'src/main/java/com/blockpedia/exporter'
    assert not (java / 'BannerRepairBase.java').exists()
    assert 'banner-repair' not in (java / 'BlockpediaExporterClient.java').read_text()
    render = (java / 'RenderExporter.java').read_text()
    assert 'instanceof BannerBlock || state.getBlock() instanceof WallBannerBlock' in render
    assert 'BANNER_PARENT_SCALE' in render
    assert '0.72f' in (java / 'ExporterConstants.java').read_text()


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
