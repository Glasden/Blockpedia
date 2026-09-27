"""Relevance gate: real building queries against a published release.

The golden set only means something on real annotations, so this runs against
the release selected by BLOCKPEDIA_GOLDEN_DATA_ROOT (default: the local data
root).  Without a published release it skips; a skip is not a pass.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import Counter
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

from blockpedia.paths import default_data_root


ROOT = Path(__file__).resolve().parents[2]
NODE = Path(os.environ.get("BLOCKPEDIA_NODE") or (
    "/opt/node-v24.21.0-linux-arm64/bin/node" if Path("/opt/node-v24.21.0-linux-arm64/bin/node").is_file() else shutil.which("node") or "node"
))
GOLDEN = json.loads((ROOT / "tests/r4/golden_queries.json").read_text(encoding="utf-8"))["queries"]
TOP_N = 8


def _data_root() -> Path:
    configured = os.environ.get("BLOCKPEDIA_GOLDEN_DATA_ROOT")
    root = Path(configured) if configured else default_data_root()
    if not (root / "current.json").is_file():
        pytest.skip(f"no published release under {root}; set BLOCKPEDIA_GOLDEN_DATA_ROOT")
    return root


def run_queries(data_root: Path, queries: list[dict]) -> list[list[str]]:
    """Top-N block IDs per query ({keywords?, similar_to?}) through the real
    ranking code (no images)."""
    script = f"""import {{ readFileSync }} from 'node:fs';
const {{ MCPQueryService }} = await import({json.dumps((ROOT / 'mcp-node/query.mjs').as_uri())});
const service = new MCPQueryService({json.dumps(str(data_root))});
service._contactSheet = () => ({{ image: {{}}, webp: new Uint8Array() }});
const out = JSON.parse(readFileSync(0, 'utf8')).map((query) => {{
  const result = service.searchBlocks({{ ...query, limit: {TOP_N} }});
  if (result.isError) throw new Error(JSON.stringify(result.structuredContent));
  return result.structuredContent.candidates.map((item) => item.block_id);
}});
process.stdout.write(JSON.stringify(out));
"""
    result = subprocess.run(
        [str(NODE), "--no-warnings", "--input-type=module", "-e", script],
        input=json.dumps(queries).encode(), capture_output=True, check=True,
    )
    return json.loads(result.stdout)


def _matches(block_id: str, patterns: list[str]) -> bool:
    name = block_id.removeprefix("minecraft:")
    return any(fnmatchcase(name, pattern) for pattern in patterns)


def evaluate(entry: dict, ranked: list[str]) -> list[str]:
    problems = []
    top3 = ranked[:3]
    if len(top3) < 3:
        problems.append(f"only {len(top3)} results")
    if entry.get("top1") and (not ranked or ranked[0] != "minecraft:" + entry["top1"]):
        problems.append(f"top1 is not {entry['top1']}")
    problems.extend(f"{block_id} in top 3 is not acceptable" for block_id in top3 if not _matches(block_id, entry["accept"]))
    problems.extend(f"{block_id} is rejected but ranked #{index}" for index, block_id in enumerate(ranked, 1) if _matches(block_id, entry["reject"]))
    problems.extend(f"nothing matching {pattern} in the top {TOP_N}" for pattern in entry.get("require", []) if not any(_matches(block_id, [pattern]) for block_id in ranked))
    if "max_per_series" in entry:
        series = Counter(_series(block_id) for block_id in ranked)
        series.pop(None, None)
        problems.extend(f"{count} members of *_{key}" for key, count in series.items() if count > entry["max_per_series"])
    return problems


DYES = ("white", "light_gray", "gray", "black", "brown", "red", "orange", "yellow", "lime", "green", "cyan", "light_blue", "blue", "purple", "magenta", "pink")


def _series(block_id: str) -> str | None:
    name = block_id.removeprefix("minecraft:")
    dye = next((dye for dye in sorted(DYES, key=len, reverse=True) if name.startswith(dye + "_")), None)
    return None if dye is None else name[len(dye) + 1:]


def test_golden_building_queries() -> None:
    if not NODE.is_file():
        pytest.skip("Node 24 is unavailable")
    queries = [{key: entry[key] for key in ("keywords", "similar_to") if key in entry} for entry in GOLDEN]
    results = run_queries(_data_root(), queries)
    failures = []
    for query, entry, ranked in zip(queries, GOLDEN, results, strict=True):
        problems = evaluate(entry, ranked)
        if problems:
            shown = " ".join(block_id.removeprefix("minecraft:") for block_id in ranked)
            failures.append(f"{query}: {'; '.join(problems)}\n    got: {shown}")
    assert not failures, f"{len(failures)}/{len(GOLDEN)} golden queries failed:\n" + "\n".join(failures)


# Material groups a builder relies on: log, wood, stripped and planks of one
# tree; the finishes of one stone.
FAMILIES = {
    "spruce_log": ["spruce_log", "spruce_planks", "spruce_wood", "stripped_spruce_log", "stripped_spruce_wood"],
    "stripped_spruce_log": ["spruce_log", "spruce_planks", "spruce_wood", "stripped_spruce_log", "stripped_spruce_wood"],
    "spruce_stairs": ["spruce_log", "spruce_planks", "spruce_wood", "stripped_spruce_log", "stripped_spruce_wood"],
    "crimson_hyphae": ["crimson_hyphae", "crimson_planks", "crimson_stem", "stripped_crimson_hyphae", "stripped_crimson_stem"],
    "tuff_brick_wall": ["chiseled_tuff", "chiseled_tuff_bricks", "polished_tuff", "tuff", "tuff_bricks"],
    "mossy_stone_bricks": ["chiseled_stone_bricks", "cracked_stone_bricks", "mossy_stone_bricks", "smooth_stone", "stone", "stone_bricks"],
    "exposed_cut_copper": ["exposed_chiseled_copper", "exposed_copper", "exposed_copper_grate", "exposed_cut_copper"],
}


def test_material_families_on_release() -> None:
    if not NODE.is_file():
        pytest.skip("Node 24 is unavailable")
    script = f"""import {{ readFileSync }} from 'node:fs';
const {{ MCPQueryService }} = await import({json.dumps((ROOT / 'mcp-node/query.mjs').as_uri())});
const service = new MCPQueryService({json.dumps(str(_data_root()))});
const out = JSON.parse(readFileSync(0, 'utf8')).map((id) => service.getBlockDetails({{ block_id: id }}).structuredContent.family ?? null);
process.stdout.write(JSON.stringify(out));
"""
    ids = ["minecraft:" + block_id for block_id in FAMILIES] + ["minecraft:bamboo", "minecraft:infested_stone"]
    result = subprocess.run([str(NODE), "--no-warnings", "--input-type=module", "-e", script], input=json.dumps(ids).encode(), capture_output=True, check=True)
    families = dict(zip(ids, json.loads(result.stdout), strict=True))
    for block_id, expected in FAMILIES.items():
        family = families["minecraft:" + block_id]
        assert ["minecraft:" + template.replace("*", family["material"]) for template in family["material_blocks"]] == ["minecraft:" + value for value in expected], block_id
    # The bamboo plant is not bamboo wood; infested stone is not stone.
    assert families["minecraft:bamboo"] is None or "material_blocks" not in families["minecraft:bamboo"]
    assert families["minecraft:infested_stone"] is None or "material_blocks" not in families["minecraft:infested_stone"]
