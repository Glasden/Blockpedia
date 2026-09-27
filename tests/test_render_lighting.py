"""Opt-in checks over fresh GPU probe output; no game assets enter the repository.

Run with BLOCKPEDIA_RENDER_EVIDENCE pointing to the probe's output directory.
Build renderLightingProbeJar, place that jar alongside the exporter in an isolated
Fabric game directory, set -Dblockpedia.probe.output to a fresh output directory,
and load a test world. The client exits after producing PASS.txt or FAIL.txt.
The probe renders the same blocks after ITEMS_FLAT and ENTITY_IN_UI lighting,
storing the second pass under alternate/. This checks artifacts, not a GPU run.
"""

import math
import os
from pathlib import Path

import pytest

from blockpedia.png import decode_rgba_png


def test_gpu_export_uses_fixed_world_lighting() -> None:
    evidence = os.environ.get("BLOCKPEDIA_RENDER_EVIDENCE")
    if not evidence:
        pytest.skip("fresh Minecraft GPU probe output not supplied")
    root = Path(evidence)
    assert (root / "PASS.txt").is_file() and not (root / "FAIL.txt").exists()
    # Vanilla DEFAULT light vectors: normalize(+/-0.2, 1, -/+0.7).
    length = math.sqrt(0.2**2 + 1 + 0.7**2)
    front_factor = 0.4 + 0.6 * 0.7 / length
    side_factor = 0.4 + 0.6 * 0.2 / length
    for preview in (root / "renders/minecraft").glob("*/preview.png"):
        assert preview.read_bytes() == (root / "alternate" / preview.relative_to(root)).read_bytes()
    for block, minimum_top in (("snow_block", 240), ("white_wool", 225), ("white_concrete", 200)):
        relative = Path("renders/minecraft") / block / "preview.png"
        payload = (root / relative).read_bytes()
        assert payload == (root / "alternate" / relative).read_bytes(), block
        image = decode_rgba_png(payload)
        assert (image.width, image.height) == (512, 512)
        means = []
        for left, top in ((256, 0), (0, 256), (256, 256)):
            pixels = [
                image.pixels[offset:offset + 3]
                for y in range(top, top + 256)
                for x in range(left, left + 256)
                if image.pixels[(offset := (y * image.width + x) * 4) + 3] == 255
            ]
            assert pixels, block
            means.append([sum(pixel[c] for pixel in pixels) / len(pixels) for c in range(3)])
        front, side, top = means
        assert min(top) >= minimum_top, (block, top)
        for channel in range(3):
            assert front[channel] / top[channel] == pytest.approx(front_factor, abs=0.01), block
            assert side[channel] / top[channel] == pytest.approx(side_factor, abs=0.01), block
