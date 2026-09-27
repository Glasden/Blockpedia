"""Measure the real EXTRACT_FEATURES worker path on a fixed release sample.

Run with PYTHONPATH=src and the intended Python interpreter. Import, validation,
and release building are outside the timed stage. Each worker count gets a fresh
workspace and a fresh interpreter; its cold start and spawned workers are timed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


COMMON = (
    "snow_block", "white_wool", "white_concrete", "oak_stairs",
    "spruce_trapdoor", "stone", "mossy_cobblestone", "glass",
    "glass_pane", "iron_bars", "oak_leaves", "brown_banner",
)
RUN_ID = "run_feature_benchmark"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def select_rows(release, count):
    from blockpedia.paths import safe_relative_posix_ref

    index = release / "index.sqlite3"
    with sqlite3.connect(f"file:{index}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = {row["variant_id"]: dict(row) for row in db.execute(
            "SELECT variant_id,block_id,record_json,preview_path,mask_path,image_sha256,mask_sha256 "
            "FROM visual_variants ORDER BY variant_id"
        )}
        fixed = ["minecraft:" + name for name in COMMON]
        missing = set(fixed) - rows.keys()
        if missing:
            raise ValueError(f"release lacks common samples: {sorted(missing)}")
        if not len(fixed) <= count <= len(rows):
            raise ValueError(f"samples must be between {len(fixed)} and {len(rows)}")
        rest = sorted(rows.keys() - set(fixed))
        extra = count - len(fixed)
        ids = sorted(fixed + [rest[(2 * i + 1) * len(rest) // (2 * extra)] for i in range(extra)])
        blocks = {}
        for block_id in {rows[variant_id]["block_id"] for variant_id in ids}:
            row = db.execute("SELECT record_json FROM blocks WHERE block_id=?", (block_id,)).fetchone()
            if row is None:
                raise ValueError(f"missing block: {block_id}")
            blocks[block_id] = row["record_json"]
    selected = []
    for variant_id in ids:
        row = rows[variant_id]
        record = json.loads(row["record_json"])
        machine = record["machine_facts"]
        machine["shape"] = machine["geometry"]["shape"]
        machine["collision"] = machine["geometry"]["collision"]
        for field in ("preview_path", "mask_path"):
            ref = safe_relative_posix_ref(row[field])
            image = release / ref
            if not image.is_file() or not image.resolve().is_relative_to(release.resolve()):
                raise ValueError(f"unsafe or missing release image: {ref}")
            safe_relative_posix_ref(record["render"][field])
        selected.append((row, record))
    return selected, blocks


def seed(root, release, workers, selected, blocks):
    from blockpedia.paths import DataRoot
    from blockpedia.storage import WorkspaceDatabase, utc_now

    workspace = DataRoot(root).workspace_dir("26.2", RUN_ID)
    workspace.mkdir(parents=True)
    identities = []
    with WorkspaceDatabase.open(workspace / "work.sqlite3") as database:
        with database.transaction() as db:
            now = utc_now()
            first = selected[0][1]
            db.execute(
                "INSERT INTO imports(import_id,minecraft_version,export_id,source_directory_ref,manifest_sha256,checksum_sha256,expected_files_json,report_json,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("import_benchmark", "26.2", first["export_id"], "benchmark", "", "", "[]", "{}", "passed", now),
            )
            db.execute(
                "INSERT INTO runs(run_id,import_id,minecraft_version,status,current_stage,config_snapshot_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (RUN_ID, "import_benchmark", "26.2", "pending", "EXTRACT_FEATURES", canonical({"feature_workers": workers}), now),
            )
            db.execute(
                "INSERT INTO stage_runs(run_id,stage,ordinal,status) VALUES (?,?,?,?)",
                (RUN_ID, "EXTRACT_FEATURES", 5, "pending"),
            )
            for block_id, record_json in blocks.items():
                db.execute("INSERT INTO blocks(block_id,minecraft_version,record_json) VALUES (?,?,?)", (block_id, "26.2", record_json))
            for row, source in selected:
                source_json = canonical(source)
                db.execute(
                    "INSERT INTO variants(variant_id,block_id,minecraft_version,status,source_json) VALUES (?,?,?,?,?)",
                    (row["variant_id"], row["block_id"], "26.2", "selected", source_json),
                )
                identities.append({
                    "variant_id": row["variant_id"],
                    "source_sha256": digest(source),
                    "image_sha256": row["image_sha256"],
                    "mask_sha256": row["mask_sha256"],
                })
    for row, source in selected:
        for field, hash_field in (("preview_path", "image_sha256"), ("mask_path", "mask_sha256")):
            target = workspace / source["render"][field]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(release / row[field], target)
            actual = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != row[hash_field]:
                raise ValueError(f"release image hash mismatch: {row['variant_id']} {field}")
    return identities


def child(root, expected):
    from blockpedia.paths import DataRoot
    from blockpedia.storage import WorkspaceDatabase
    from blockpedia.worker import WorkerService, shutdown_process_ai_executor

    service = WorkerService(DataRoot(root))
    deadline = time.monotonic() + 600
    try:
        while time.monotonic() < deadline:
            service.tick(RUN_ID, "26.2")
            with service.open_database(RUN_ID, "26.2") as db:
                stage = db.fetchone("SELECT status FROM stage_runs WHERE run_id=? AND stage='EXTRACT_FEATURES'", (RUN_ID,))
                if stage["status"] in ("succeeded", "failed"):
                    break
            time.sleep(0.02)
        else:
            raise TimeoutError("EXTRACT_FEATURES did not finish in 600 seconds")
        with service.open_database(RUN_ID, "26.2") as db:
            jobs = db.fetchone("SELECT COUNT(*) AS n FROM jobs WHERE stage='EXTRACT_FEATURES' AND status='succeeded'")["n"]
            status = db.fetchone("SELECT status FROM stage_runs WHERE run_id=? AND stage='EXTRACT_FEATURES'", (RUN_ID,))["status"]
            if status != "succeeded" or jobs != expected:
                failures = [dict(row) for row in db.fetchall("SELECT logical_key,error_code,error_message FROM jobs WHERE status='failed' LIMIT 5")]
                raise RuntimeError(f"stage={status}, succeeded={jobs}/{expected}, failures={failures}")
            outputs = [{"variant_id": row["variant_id"], "features": json.loads(row["feature_json"]), "record": json.loads(row["record_json"])}
                       for row in db.fetchall("SELECT v.variant_id,f.feature_json,v.record_json FROM variants v JOIN features f USING (variant_id) ORDER BY v.variant_id")]
            if len(outputs) != expected:
                raise RuntimeError(f"feature rows={len(outputs)}/{expected}")
    finally:
        if not service.close(timeout=30):
            raise RuntimeError("worker service did not close")
        shutdown_process_ai_executor()
    cpu = os.times()
    print(canonical({
        "output_digest": digest(outputs),
        "item_output_digests": {item["variant_id"]: digest(item) for item in outputs},
        "succeeded_jobs": jobs,
        "cpu_seconds_self_children": round(cpu.user + cpu.system + cpu.children_user + cpu.children_system, 3),
    }), flush=True)


def tree_rss_kib(root_pid):
    """Instantaneous sum of VmRSS for a live Linux process tree, in KiB."""
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    entries = {}
    for path in proc.iterdir():
        if not path.name.isdigit():
            continue
        try:
            lines = (path / "status").read_text().splitlines()
            fields = dict(line.split(":", 1) for line in lines if ":" in line)
            entries[int(path.name)] = (int(fields["PPid"].strip()), int(fields.get("VmRSS", "0 kB").split()[0]))
        except (OSError, KeyError, ValueError):
            continue
    live = {root_pid}
    while True:
        found = live | {pid for pid, (parent, _) in entries.items() if parent in live}
        if found == live:
            break
        live = found
    return sum(entries[pid][1] for pid in live if pid in entries)


def run(release, counts, samples, evidence):
    selected, blocks = select_rows(release, samples)
    sources = {name: "sha256:" + hashlib.sha256((Path(__file__).resolve().parents[1] / "src/blockpedia" / name).read_bytes()).hexdigest()
               for name in ("worker.py", "features.py")}
    report = {
        "release": str(release), "release_id": release.name,
        "scope": "EXTRACT_FEATURES only; real release preview/mask and record facts; import/validation excluded",
        "wall_scope": "fresh interpreter startup through tick, commit, worker shutdown and result digest; workspace seeding excluded",
        "rss_scope": "sampled maximum of summed live process-tree VmRSS (KiB); Linux /proc, 20 ms polling; may miss short peaks",
        "environment": {
            "python": sys.version.split()[0], "executable": sys.executable,
            "architecture": platform.machine(), "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "memory_total_kib": next((int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemTotal:")), None) if Path("/proc/meminfo").exists() else None,
        },
        "sample_count": samples,
        "sample_rule": "12 fixed PROGRESS 3.5 ids plus evenly spaced sorted variant_id remainder",
        "sample_ids": [row["variant_id"] for row, _ in selected],
        "core_source_sha256": sources,
        "runs": [],
    }
    evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="feature-workers-", dir=evidence) as temporary:
        for workers in counts:
            root = Path(temporary) / str(workers)
            identities = seed(root, release, workers, selected, blocks)
            if "input_identities" not in report:
                report["input_identities"] = identities
                report["input_digest"] = digest(identities)
            elif identities != report["input_identities"]:
                raise RuntimeError("input identity differs between worker counts")
            started = time.perf_counter()
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child", str(root), str(samples)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            peak = 0
            while process.poll() is None:
                reading = tree_rss_kib(process.pid)
                if reading is not None:
                    peak = max(peak, reading)
                time.sleep(0.02)
            stdout, stderr = process.communicate()
            wall = time.perf_counter() - started
            if process.returncode:
                raise RuntimeError(f"workers={workers}, exit={process.returncode}: {stderr[-3000:]}")
            result = json.loads(stdout)
            result.update({"workers": workers, "stage_wall_seconds": round(wall, 3),
                           "peak_tree_rss_kib": peak if Path("/proc").is_dir() else None})
            report["runs"].append(result)
            current = {name: "sha256:" + hashlib.sha256((Path(__file__).resolve().parents[1] / "src/blockpedia" / name).read_bytes()).hexdigest()
                       for name in sources}
            if current != sources:
                raise RuntimeError("worker/features source changed during benchmark")
            print(f"workers={workers} wall={wall:.3f}s CPU={result['cpu_seconds_self_children']:.3f}s peakRSS={result['peak_tree_rss_kib']}KiB", flush=True)
    report["outputs_equal"] = len({item["output_digest"] for item in report["runs"]}) == 1 if len(report["runs"]) > 1 else None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = evidence / f"benchmark-{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report["outputs_equal"] is False:
        raise RuntimeError(f"feature/record output digests differ across worker counts; evidence={path}")
    print(f"outputs_equal={report['outputs_equal']} evidence={path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, default=Path("/home/ubuntu/.local/share/blockpedia/releases/26.2/rel_d0204fb090764c94b4a868a29ec50894"))
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 5])
    parser.add_argument("--samples", type=int, default=48)
    parser.add_argument("--evidence", type=Path, default=Path("build/feature-workers-evidence"))
    parser.add_argument("--child", nargs=2, metavar=("ROOT", "COUNT"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child(Path(args.child[0]), int(args.child[1]))
    else:
        if not args.release.is_dir():
            parser.error("release directory does not exist")
        if not args.workers or any(worker not in (1, 2, 5) for worker in args.workers) or len(set(args.workers)) != len(args.workers):
            parser.error("workers must be unique values from 1, 2, 5")
        run(args.release.resolve(), args.workers, args.samples, args.evidence)


if __name__ == "__main__":
    main()
