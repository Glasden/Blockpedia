"""Keep historical mixed export IDs without replaying historical AI inputs."""
import json
import sqlite3

import pytest
from blockpedia.releases import ReleaseBuildFailure
from .test_release_builder import _ready

COLORS = 'black blue brown cyan gray green light_blue light_gray lime magenta orange pink purple red white yellow'.split()
TARGETS = sorted(f'minecraft:{color}_{form}' for color in COLORS for form in ('banner', 'wall_banner'))


@pytest.mark.parametrize('target_is_preserved', [False, True])
def test_historical_export_relation_uses_existing_records(tmp_path, target_is_preserved):
    service, run_id = _ready(tmp_path)
    try:
        with service.worker.open_database(run_id) as database:
            source = dict(database.fetchone('SELECT * FROM imports'))
            envelope = json.loads(database.fetchone('SELECT envelope_json FROM provider_requests LIMIT 1')[0])
        base = 'export_20260814T115959Z'
        envelope['export_id'] = base
        if target_is_preserved:
            envelope['input_summary']['tile_variant_map'][0]['variant_id'] = TARGETS[0]
        provenance = {'format': 'banner-refresh.v1', 'base': {'export_id': base},
                      'new': {'export_id': source['export_id'], 'import_id': source['import_id']}, 'target_ids': TARGETS}
        with sqlite3.connect(':memory:') as c:
            c.row_factory = sqlite3.Row
            c.execute('CREATE TABLE provider_requests(envelope_json TEXT, stage TEXT, status TEXT)')
            c.execute('INSERT INTO provider_requests VALUES (?, ?, ?)', (json.dumps(envelope), 'offline_annotation', 'succeeded'))
            variants = dict.fromkeys(['minecraft:stone', *TARGETS], {})
            if target_is_preserved:
                with pytest.raises(ReleaseBuildFailure):
                    service.release_builder._validate_lineage(c, provenance, source, variants)
            else:
                service.release_builder._validate_lineage(c, provenance, source, variants)
    finally:
        service.close()
