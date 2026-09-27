from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import Future
from pathlib import Path

import pytest

from blockpedia.features import compute_visual_variant
from blockpedia.paths import DataRoot
from blockpedia.services import StudioService
from blockpedia.stages import RunStateConflict
from blockpedia.worker import WorkerService
from tests.import_helpers import import_export
from tests.r2.conftest import PassingToolchainProbe, make_export


def observed_compute(source, preview, mask, geometry, tags):
    marker = os.environ.get("BLOCKPEDIA_FEATURE_TEST_MARKER")
    if marker:
        with open(marker, "ab", buffering=0) as stream:
            stream.write(f"start {os.getpid()} {time.monotonic()}\n".encode())
    time.sleep(0.35)
    result = compute_visual_variant(source, preview, mask, geometry, tags)
    if marker:
        with open(marker, "ab", buffering=0) as stream:
            stream.write(f"end {os.getpid()} {time.monotonic()}\n".encode())
    return result


def failing_compute(source, preview, mask, geometry, tags):
    raise ValueError("fixture child failure")


def crashing_compute(source, preview, mask, geometry, tags):
    gate = Path(os.environ["BLOCKPEDIA_CRASH_GATE"])
    deadline = time.monotonic() + 10
    while not gate.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if gate.exists():
        os._exit(17)
    raise RuntimeError("crash gate was not released")


def gated_compute(source, preview, mask, geometry, tags):
    marker = Path(os.environ["BLOCKPEDIA_COMPUTE_MARKER"])
    gate = Path(os.environ["BLOCKPEDIA_COMPUTE_GATE"])
    with marker.open("ab", buffering=0) as stream:
        stream.write(f"{os.getpid()}\n".encode())
    deadline = time.monotonic() + 15
    while not gate.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not gate.exists():
        raise RuntimeError("compute gate was not released")
    return compute_visual_variant(source, preview, mask, geometry, tags)


def split_failure_compute(source, preview, mask, geometry, tags):
    if source["variant_id"] == "minecraft:stone":
        raise ValueError("first item fails")
    gate = Path(os.environ["BLOCKPEDIA_COMPUTE_GATE"])
    deadline = time.monotonic() + 15
    while not gate.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not gate.exists():
        raise RuntimeError("sibling gate was not released")
    return compute_visual_variant(source, preview, mask, geometry, tags)


def setup_run(tmp_path: Path, workers: int, variants: int = 5):
    service = StudioService(DataRoot(tmp_path), toolchain_probe=PassingToolchainProbe())
    imported = import_export(service, make_export(tmp_path))
    run_id = imported["run_id"]
    with service.worker.open_database(run_id) as database:
        with database.transaction() as connection:
            connection.execute("UPDATE runs SET config_snapshot_json=? WHERE run_id=?", (json.dumps({"feature_workers": workers}), run_id))
            original = connection.execute("SELECT * FROM variants WHERE status='selected'").fetchone()
            for index in range(1, variants):
                source = json.loads(original["source_json"])
                source["variant_id"] = f"minecraft:stone_test_{index}"
                connection.execute("INSERT INTO variants(variant_id,block_id,minecraft_version,status,source_json) VALUES (?,?,?,?,?)", (source["variant_id"], original["block_id"], original["minecraft_version"], "selected", json.dumps(source)))
    return service, run_id


def pump(service, run_id, *, until="R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING", timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = service.get_run(run_id)
        if run["boundary_event"] == until or run["status"] in {"failed", "cancelled"}:
            return run
        service.tick(run_id)
        time.sleep(0.02)
    raise AssertionError("feature stage did not finish")


def feature_rows(service, run_id):
    with service.worker.open_database(run_id) as database:
        return [(row["variant_id"], row["feature_json"], row["output_hash"]) for row in database.fetchall("SELECT * FROM features ORDER BY variant_id")]


def test_feature_workers_1_2_5_equal_and_spawn_overlap(monkeypatch, tmp_path):
    import blockpedia.worker as module
    monkeypatch.setattr(module, "compute_visual_variant", observed_compute)
    marker = tmp_path / "timings.txt"
    monkeypatch.setenv("BLOCKPEDIA_FEATURE_TEST_MARKER", str(marker))
    results = {}
    for workers in (1, 2, 5):
        service, run_id = setup_run(tmp_path / str(workers), workers)
        try:
            assert pump(service, run_id)["boundary_event"] == "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING"
            results[workers] = feature_rows(service, run_id)
            with service.worker.open_database(run_id) as database:
                assert database.fetchone("SELECT COUNT(*) AS n FROM jobs WHERE stage='EXTRACT_FEATURES' AND status='succeeded'")["n"] == 5
                assert database.fetchone("SELECT COUNT(*) AS n FROM audit_events WHERE event_type='FEATURE_ITEM_SUCCEEDED'")["n"] == 5
        finally:
            assert service.close(timeout=10)
    assert results[1] == results[2] == results[5]
    starts = {}
    ends = {}
    for kind, pid, instant in (line.split() for line in marker.read_text().splitlines()):
        (starts if kind == "start" else ends).setdefault(pid, []).append(float(instant))
    assert len(starts) >= 2
    assert any(starts[left][0] < ends[right][-1] and starts[right][0] < ends[left][-1] for left in starts for right in starts if left != right)


def test_feature_pause_cancel_close_and_stale(monkeypatch, tmp_path):
    import blockpedia.worker as module
    monkeypatch.setattr(module, "compute_visual_variant", observed_compute)
    service, run_id = setup_run(tmp_path / "pause", 2)
    try:
        service.tick(run_id)
        assert service.pause(run_id)["status"] == "running"
        deadline = time.monotonic() + 10
        while service.get_run(run_id)["status"] != "paused" and time.monotonic() < deadline:
            service.tick(run_id)
            time.sleep(0.02)
        assert service.get_run(run_id)["status"] == "paused"
        with service.worker.open_database(run_id) as database:
            assert database.fetchone("SELECT COUNT(*) AS n FROM jobs WHERE stage='EXTRACT_FEATURES' AND status='running'")["n"] == 0
            assert database.fetchone("SELECT COUNT(*) AS n FROM jobs WHERE stage='EXTRACT_FEATURES' AND status='succeeded'")["n"] == 2
            assert database.fetchone("SELECT COUNT(*) AS n FROM jobs WHERE stage='EXTRACT_FEATURES' AND status='pending'")["n"] == 3
        service.resume(run_id)
        assert pump(service, run_id)["boundary_event"] == "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING"
    finally:
        assert service.close(timeout=10)

    service, run_id = setup_run(tmp_path / "cancel", 5)
    try:
        service.tick(run_id)
        assert service.close(timeout=0.01) is False
        assert service.cancel(run_id)["status"] == "cancelled"
        assert service.close(timeout=10)
        with service.worker.open_database(run_id) as database:
            assert database.fetchone("SELECT COUNT(*) AS n FROM features")["n"] == 0
            assert database.fetchone("SELECT COUNT(*) AS n FROM jobs WHERE stage='EXTRACT_FEATURES' AND status='running'")["n"] == 0
    finally:
        service.close(timeout=10)

    service, run_id = setup_run(tmp_path / "stale", 2, variants=1)
    try:
        service.tick(run_id)
        with service.worker.open_database(run_id) as database:
            database.execute("UPDATE jobs SET heartbeat_at='2000-01-01T00:00:00Z' WHERE stage='EXTRACT_FEATURES'")
        assert service.worker.detect_stale(run_id) == []
        with pytest.raises(RunStateConflict):
            service.recover(run_id, stage="EXTRACT_FEATURES")
        assert pump(service, run_id)["boundary_event"] == "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING"
    finally:
        assert service.close(timeout=10)


def test_feature_child_failure_is_transactional(monkeypatch, tmp_path):
    import blockpedia.worker as module
    monkeypatch.setattr(module, "compute_visual_variant", failing_compute)
    service, run_id = setup_run(tmp_path, 2)
    try:
        assert pump(service, run_id)["status"] == "failed"
        assert service.close(timeout=10)
        with service.worker.open_database(run_id) as database:
            assert database.fetchone("SELECT COUNT(*) AS n FROM features")["n"] == 0
            assert database.fetchone("SELECT COUNT(*) AS n FROM jobs WHERE stage='EXTRACT_FEATURES' AND status='running'")["n"] == 0
    finally:
        service.close(timeout=10)


def test_broken_process_pool_joins_before_close_and_replacement(monkeypatch, tmp_path):
    import blockpedia.worker as module
    crash_gate = tmp_path / "crash-now"
    monkeypatch.setenv("BLOCKPEDIA_CRASH_GATE", str(crash_gate))
    monkeypatch.setattr(module, "compute_visual_variant", crashing_compute)
    shutdown_entered, release_shutdown = threading.Event(), threading.Event()
    original_shutdown = module.ProcessPoolExecutor.shutdown

    def gated_shutdown(executor, *args, **kwargs):
        if not shutdown_entered.is_set():
            shutdown_entered.set()
            assert release_shutdown.wait(10)
        return original_shutdown(executor, *args, **kwargs)

    monkeypatch.setattr(module.ProcessPoolExecutor, "shutdown", gated_shutdown)
    service, run_id = setup_run(tmp_path / "crashed", 2, variants=1)
    next_service = None
    try:
        service.tick(run_id)
        pool = module._PROCESS_COORDINATOR.feature_executor
        assert pool is not None
        processes = list(pool._processes.values())
        assert processes
        crash_gate.touch()
        assert shutdown_entered.wait(10)
        with module._PROCESS_COORDINATOR.changed:
            deadline = time.monotonic() + 10
            while service.worker.has_live_feature_futures(run_id) and time.monotonic() < deadline:
                module._PROCESS_COORDINATOR.changed.wait(0.1)
        assert not service.worker.has_live_feature_futures(run_id)
        assert service.get_run(run_id)["status"] == "failed"
        assert service.close(timeout=0.05) is False
        assert module._PROCESS_COORDINATOR.feature_shutdown is not None

        next_service, next_run = setup_run(tmp_path / "replacement", 2, variants=1)
        next_service.tick(next_run)
        with next_service.worker.open_database(next_run) as database:
            assert database.fetchone("SELECT status FROM jobs WHERE stage='EXTRACT_FEATURES'")["status"] == "pending"
        assert module._PROCESS_COORDINATOR.feature_executor is None

        release_shutdown.set()
        assert service.close(timeout=10) is True
        assert all(not process.is_alive() for process in processes)
        monkeypatch.setattr(module, "compute_visual_variant", compute_visual_variant)
        assert pump(next_service, next_run)["boundary_event"] == "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING"
        assert len(feature_rows(next_service, next_run)) == 1
    finally:
        release_shutdown.set()
        service.close(timeout=10)
        if next_service is not None:
            next_service.close(timeout=10)


def test_shared_pool_caps_across_runs_and_workers(monkeypatch, tmp_path):
    import blockpedia.worker as module
    monkeypatch.setattr(module, "compute_visual_variant", observed_compute)
    first, run_a = setup_run(tmp_path / "a", 5)
    second, run_b = setup_run(tmp_path / "b", 5)
    extra = WorkerService(first.data_root, toolchain_probe=PassingToolchainProbe())
    try:
        first.tick(run_a)
        extra.tick(run_a)
        second.tick(run_b)
        assert first.worker._feature_counts(run_a)[0] <= 5
        maximum = 0
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            first.tick(run_a)
            second.tick(run_b)
            maximum = max(maximum, first.worker._feature_counts(run_a)[0])
            if all(item.get("boundary_event") == "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING" for item in (first.get_run(run_a), second.get_run(run_b))):
                break
            time.sleep(0.02)
        assert maximum == 5
        assert first.get_run(run_a)["boundary_event"] == second.get_run(run_b)["boundary_event"] == "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING"
        assert len(feature_rows(first, run_a)) == len(feature_rows(second, run_b)) == 5
        with first.worker.open_database(run_a) as database:
            assert database.fetchone("SELECT COUNT(*) AS n FROM audit_events WHERE event_type='FEATURE_ITEM_SUCCEEDED'")["n"] == 5
    finally:
        assert extra.close(timeout=10)
        assert first.close(timeout=10)
        assert second.close(timeout=10)


def test_graceful_close_releases_unfinished_feature_stage(monkeypatch, tmp_path):
    import blockpedia.worker as module
    monkeypatch.setattr(module, "compute_visual_variant", observed_compute)
    service, run_id = setup_run(tmp_path, 2)
    service.tick(run_id)
    processes = list(module._PROCESS_COORDINATOR.feature_executor._processes.values())
    assert service.close(timeout=10)
    assert processes and all(not process.is_alive() for process in processes)
    with service.worker.open_database(run_id) as database:
        assert database.fetchone("SELECT status FROM runs WHERE run_id=?", (run_id,))["status"] == "pending"
        assert database.fetchone("SELECT status,worker_id FROM stage_runs WHERE run_id=? AND stage='EXTRACT_FEATURES'", (run_id,))["worker_id"] is None
    resumed = WorkerService(service.data_root, toolchain_probe=PassingToolchainProbe())
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            resumed.tick(run_id)
            with resumed.open_database(run_id) as database:
                if database.fetchone("SELECT boundary_event FROM runs WHERE run_id=?", (run_id,))["boundary_event"]:
                    break
            time.sleep(0.02)
        with resumed.open_database(run_id) as database:
            assert database.fetchone("SELECT COUNT(*) AS n FROM features")["n"] == 5
            assert database.fetchone("SELECT COUNT(*) AS n FROM audit_events WHERE event_type='FEATURE_ITEM_SUCCEEDED'")["n"] == 5
    finally:
        assert resumed.close(timeout=10)


def test_stale_parallel_feature_job_recomputes_without_duplicate_commit(tmp_path):
    service, run_id = setup_run(tmp_path, 2, variants=1)
    try:
        with service.worker.open_database(run_id) as database:
            assert service.worker._begin_stage(database, run_id, "EXTRACT_FEATURES")
            claimed = service.worker._claim_pending_job(database, run_id, allow_parallel=True)
            assert claimed is not None
            with database.transaction() as connection:
                connection.execute("UPDATE stage_runs SET worker_id='dead_worker',heartbeat_at='2000-01-01T00:00:00Z' WHERE run_id=? AND stage='EXTRACT_FEATURES'", (run_id,))
                connection.execute("UPDATE jobs SET worker_id='dead_worker',heartbeat_at='2000-01-01T00:00:00Z' WHERE job_id=?", (claimed["job_id"],))
        assert service.worker.detect_stale(run_id)
        assert service.recover(run_id, stage="EXTRACT_FEATURES")["recovered"]["status"] == "pending"
        assert pump(service, run_id)["boundary_event"] == "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING"
        with service.worker.open_database(run_id) as database:
            assert database.fetchone("SELECT COUNT(*) AS n FROM features")["n"] == 1
            assert database.fetchone("SELECT COUNT(*) AS n FROM audit_events WHERE event_type='FEATURE_ITEM_SUCCEEDED'")["n"] == 1
            assert database.fetchone("SELECT auto_attempt FROM jobs WHERE job_id=?", (claimed["job_id"],))["auto_attempt"] == 1
    finally:
        assert service.close(timeout=10)


def test_coordinator_heartbeats_during_spawn_compute(monkeypatch, tmp_path):
    import blockpedia.worker as module
    monkeypatch.setattr(module, "compute_visual_variant", observed_compute)
    service, run_id = setup_run(tmp_path, 2)
    try:
        service.tick(run_id)
        with service.worker.open_database(run_id) as database:
            before = database.fetchone("SELECT heartbeat_at FROM stage_runs WHERE run_id=? AND stage='EXTRACT_FEATURES'", (run_id,))["heartbeat_at"]
        assert service.worker.start(interval_seconds=0.01)
        deadline = time.monotonic() + 5
        updated = False
        while time.monotonic() < deadline:
            with service.worker.open_database(run_id) as database:
                stage = database.fetchone("SELECT status,heartbeat_at FROM stage_runs WHERE run_id=? AND stage='EXTRACT_FEATURES'", (run_id,))
                updated = stage["status"] == "running" and stage["heartbeat_at"] > before
            if updated:
                break
            time.sleep(0.02)
        assert updated
    finally:
        assert service.close(timeout=10)


def test_pause_uses_committed_running_jobs_when_callbacks_overlap(monkeypatch, tmp_path):
    import blockpedia.worker as module
    service, run_id = setup_run(tmp_path, 2, variants=3)
    parked, release = threading.Event(), threading.Event()
    entries = []
    threads = []
    try:
        with service.worker.open_database(run_id) as database:
            assert service.worker._begin_stage(database, run_id, "EXTRACT_FEATURES")
            for _ in range(2):
                job = service.worker._claim_pending_job(database, run_id, allow_parallel=True)
                source = json.loads(database.fetchone("SELECT source_json FROM variants WHERE variant_id=?", (job["logical_key"],))["source_json"])
                base = database.path.parent
                render = source["render"]
                result = compute_visual_variant(source, (base / render["preview_path"]).read_bytes(), (base / render["mask_path"]).read_bytes(), module._geometry_from_source(source), list(module._source_machine_tags(source)))
                future = Future()
                future.set_result(result)
                entry = module._RegisteredFeatureTask(service.worker._root_key, run_id, job["job_id"], service.worker.worker_id, job["logical_key"], future)
                entries.append((entry, future))
        with module._PROCESS_COORDINATOR.lock:
            for entry, _ in entries:
                module._PROCESS_COORDINATOR.feature_registry[entry.task_key] = entry
        assert service.pause(run_id)["status"] == "running"
        original_finish = service.worker._finish_extract_stage

        def parked_finish(database, target_run_id):
            if threading.current_thread().name == "first-feature-callback":
                parked.set()
                assert release.wait(5)
            return original_finish(database, target_run_id)

        monkeypatch.setattr(service.worker, "_finish_extract_stage", parked_finish)
        first = threading.Thread(target=service.worker._feature_done, args=entries[0], name="first-feature-callback")
        second = threading.Thread(target=service.worker._feature_done, args=entries[1], name="second-feature-callback")
        threads = [first, second]
        first.start()
        assert parked.wait(5)
        second.start()
        second.join(5)
        assert not second.is_alive()
        with service.worker.open_database(run_id) as database:
            assert database.fetchone("SELECT status FROM runs WHERE run_id=?", (run_id,))["status"] == "paused"
            assert database.fetchone("SELECT COUNT(*) AS n FROM jobs WHERE stage='EXTRACT_FEATURES' AND status='succeeded'")["n"] == 2
            assert database.fetchone("SELECT COUNT(*) AS n FROM jobs WHERE stage='EXTRACT_FEATURES' AND status='pending'")["n"] == 1
            assert database.fetchone("SELECT COUNT(*) AS n FROM audit_events WHERE event_type='RUN_PAUSED_AFTER_ITEM'")["n"] == 1
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
        with module._PROCESS_COORDINATOR.lock:
            for entry, _ in entries:
                module._PROCESS_COORDINATOR.feature_registry.pop(entry.task_key, None)
            module._PROCESS_COORDINATOR.changed.notify_all()
        assert service.close(timeout=10)


def test_default_serial_runs_overlap_without_blocking_heartbeat_or_reentry(monkeypatch, tmp_path):
    import blockpedia.worker as module
    service, run_a = setup_run(tmp_path, 1, variants=1)
    run_b = import_export(service, next((tmp_path / "exports" / "26.2").iterdir()))["run_id"]
    entered, release = threading.Event(), threading.Event()
    calls = []
    calls_lock = threading.Lock()
    original = module.extract_features
    reentry = None

    def blocked(*args, **kwargs):
        with calls_lock:
            calls.append(threading.current_thread().name)
            if len(calls) == 2:
                entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "extract_features", blocked)
    try:
        assert service.worker.start(interval_seconds=0.01)
        assert entered.wait(5)
        assert calls == ["blockpedia-feature_0", "blockpedia-feature_1"] or all(name.startswith("blockpedia-feature") for name in calls)
        with service.worker.open_database(run_a) as database:
            before = database.fetchone("SELECT heartbeat_at FROM jobs WHERE stage='EXTRACT_FEATURES'")["heartbeat_at"]
        deadline = time.monotonic() + 5
        after = before
        while after <= before and time.monotonic() < deadline:
            with service.worker.open_database(run_a) as database:
                after = database.fetchone("SELECT heartbeat_at FROM jobs WHERE stage='EXTRACT_FEATURES'")["heartbeat_at"]
            time.sleep(0.02)
        assert after > before
        assert service.worker._feature_counts(run_a) == (2, 1)

        reentry = threading.Thread(target=service.tick, args=(run_a,))
        reentry.start()
        time.sleep(0.05)
        assert reentry.is_alive() and len(calls) == 2
        assert service.close(timeout=0.05) is False
        release.set()
        reentry.join(10)
        assert not reentry.is_alive()
        assert service.close(timeout=10)
        for run_id in (run_a, run_b):
            with service.worker.open_database(run_id) as database:
                assert database.fetchone("SELECT COUNT(*) AS n FROM audit_events WHERE event_type='FEATURE_ITEM_SUCCEEDED'")["n"] == 1
    finally:
        release.set()
        if reentry is not None:
            reentry.join(10)
        service.close(timeout=10)


def test_five_process_slots_block_default_serial_sixth_task(monkeypatch, tmp_path):
    import blockpedia.worker as module
    marker = tmp_path / "process-starts.txt"
    gate = tmp_path / "release-processes"
    monkeypatch.setenv("BLOCKPEDIA_COMPUTE_MARKER", str(marker))
    monkeypatch.setenv("BLOCKPEDIA_COMPUTE_GATE", str(gate))
    monkeypatch.setattr(module, "compute_visual_variant", gated_compute)
    parallel, run_parallel = setup_run(tmp_path / "parallel", 5)
    serial, run_serial = setup_run(tmp_path / "serial", 1, variants=1)
    serial_entered, release_serial = threading.Event(), threading.Event()
    original = module.extract_features

    def blocked_serial(*args, **kwargs):
        serial_entered.set()
        assert release_serial.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "extract_features", blocked_serial)
    try:
        parallel.tick(run_parallel)
        deadline = time.monotonic() + 10
        while (not marker.exists() or len(marker.read_text().splitlines()) < 5) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(marker.read_text().splitlines()) == 5
        assert serial.worker.start(interval_seconds=0.01)
        deadline = time.monotonic() + 5
        while serial.get_run(run_serial)["status"] != "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert serial.get_run(run_serial)["status"] == "running"
        assert parallel.worker._feature_counts(run_parallel) == (5, 5)
        assert not serial_entered.wait(0.1)
        gate.touch()
        assert serial_entered.wait(10)
        assert parallel.worker._feature_counts(run_parallel)[0] <= 5
        release_serial.set()
        assert pump(parallel, run_parallel)["boundary_event"] == "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING"
        deadline = time.monotonic() + 10
        while serial.get_run(run_serial)["boundary_event"] is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert serial.get_run(run_serial)["boundary_event"] == "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING"
    finally:
        gate.touch()
        release_serial.set()
        assert parallel.close(timeout=10)
        assert serial.close(timeout=10)


def test_default_direct_tick_waits_for_shared_slot(monkeypatch, tmp_path):
    import blockpedia.worker as module
    marker = tmp_path / "starts.txt"
    gate = tmp_path / "release"
    monkeypatch.setenv("BLOCKPEDIA_COMPUTE_MARKER", str(marker))
    monkeypatch.setenv("BLOCKPEDIA_COMPUTE_GATE", str(gate))
    monkeypatch.setattr(module, "compute_visual_variant", gated_compute)
    parallel, run_parallel = setup_run(tmp_path / "parallel", 5)
    serial, run_serial = setup_run(tmp_path / "serial", 1, variants=1)
    result = []
    thread = None
    try:
        parallel.tick(run_parallel)
        assert parallel.worker._feature_counts(run_parallel) == (5, 5)
        thread = threading.Thread(target=lambda: result.append(serial.tick(run_serial)))
        thread.start()
        time.sleep(0.1)
        assert thread.is_alive() and not result
        gate.touch()
        thread.join(10)
        assert not thread.is_alive() and result[0]["boundary_event"] == "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING"
        assert len(feature_rows(serial, run_serial)) == 1
    finally:
        gate.touch()
        if thread is not None:
            thread.join(10)
        assert parallel.close(timeout=10)
        assert serial.close(timeout=10)


def test_retry_rejects_live_sibling_and_old_callback_cannot_commit_new_lease(monkeypatch, tmp_path):
    import blockpedia.worker as module
    gate = tmp_path / "release-sibling"
    monkeypatch.setenv("BLOCKPEDIA_COMPUTE_GATE", str(gate))
    monkeypatch.setattr(module, "compute_visual_variant", split_failure_compute)
    service, run_id = setup_run(tmp_path, 2, variants=2)
    try:
        service.tick(run_id)
        with module._PROCESS_COORDINATOR.lock:
            sibling = next(entry for entry in module._PROCESS_COORDINATOR.feature_registry.values() if entry.run_id == run_id and entry.logical_key.endswith("stone_test_1"))
        deadline = time.monotonic() + 10
        while service.get_run(run_id)["status"] != "failed" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert service.get_run(run_id)["status"] == "failed"
        assert service.worker.has_live_feature_futures(run_id)
        with pytest.raises(RunStateConflict, match="live feature work"):
            service.retry_failed(run_id)
        gate.touch()
        with module._PROCESS_COORDINATOR.changed:
            deadline = time.monotonic() + 10
            while service.worker.has_live_feature_futures(run_id) and time.monotonic() < deadline:
                module._PROCESS_COORDINATOR.changed.wait(0.1)
        assert not service.worker.has_live_feature_futures(run_id)
        assert service.retry_failed(run_id)["status"] == "pending"

        with service.worker.open_database(run_id) as database:
            assert service.worker._begin_stage(database, run_id, "EXTRACT_FEATURES")
            with database.transaction() as connection:
                connection.execute("UPDATE jobs SET status='running',worker_id=? WHERE job_id=? AND status='pending'", (service.worker.worker_id, sibling.job_id))
            source = json.loads(database.fetchone("SELECT source_json FROM variants WHERE variant_id=?", (sibling.logical_key,))["source_json"])
            base = database.path.parent
            render = source["render"]
            result = compute_visual_variant(source, (base / render["preview_path"]).read_bytes(), (base / render["mask_path"]).read_bytes(), module._geometry_from_source(source), list(module._source_machine_tags(source)))
        old_future = Future()
        old_future.set_result(result)
        service.worker._feature_done(sibling, old_future)
        with service.worker.open_database(run_id) as database:
            assert database.fetchone("SELECT status FROM jobs WHERE job_id=?", (sibling.job_id,))["status"] == "running"
            assert database.fetchone("SELECT COUNT(*) AS n FROM features")["n"] == 0
            with database.transaction() as connection:
                connection.execute("UPDATE jobs SET status='pending',worker_id=NULL WHERE job_id=?", (sibling.job_id,))
        monkeypatch.setattr(module, "compute_visual_variant", compute_visual_variant)
        assert pump(service, run_id)["boundary_event"] == "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING"
        assert len(feature_rows(service, run_id)) == 2
    finally:
        gate.touch()
        assert service.close(timeout=10)


def test_dispatch_preserves_existing_job_registration(tmp_path):
    import blockpedia.worker as module
    service, run_id = setup_run(tmp_path, 2, variants=1)
    entry = None
    try:
        with service.worker.open_database(run_id) as database:
            assert service.worker._begin_stage(database, run_id, "EXTRACT_FEATURES")
            job = service.worker._claim_pending_job(database, run_id, allow_parallel=True)
            entry = module._RegisteredFeatureTask(service.worker._root_key, run_id, job["job_id"], service.worker.worker_id, job["logical_key"])
            with module._PROCESS_COORDINATOR.lock:
                module._PROCESS_COORDINATOR.feature_registry[entry.task_key] = entry
            with database.transaction() as connection:
                connection.execute("UPDATE jobs SET status='pending',worker_id=NULL WHERE job_id=?", (job["job_id"],))
            service.worker._extract_parallel(database, run_id, 2)
            with module._PROCESS_COORDINATOR.lock:
                assert module._PROCESS_COORDINATOR.feature_registry[entry.task_key] is entry
            assert database.fetchone("SELECT status FROM jobs WHERE job_id=?", (job["job_id"],))["status"] == "pending"
        with module._PROCESS_COORDINATOR.lock:
            module._PROCESS_COORDINATOR.feature_registry.pop(entry.task_key)
            module._PROCESS_COORDINATOR.changed.notify_all()
        assert pump(service, run_id)["boundary_event"] == "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING"
        assert len(feature_rows(service, run_id)) == 1
    finally:
        if entry is not None:
            with module._PROCESS_COORDINATOR.lock:
                module._PROCESS_COORDINATOR.feature_registry.pop(entry.task_key, None)
                module._PROCESS_COORDINATOR.changed.notify_all()
        assert service.close(timeout=10)


def test_second_claim_failure_attaches_first_callback_and_can_retry(monkeypatch, tmp_path):
    import blockpedia.worker as module
    monkeypatch.setattr(module, "compute_visual_variant", observed_compute)
    service, run_id = setup_run(tmp_path, 2, variants=3)
    original_claim = service.worker._claim_pending_job
    claims = 0

    def fail_second(*args, **kwargs):
        nonlocal claims
        claims += 1
        if claims == 2:
            raise RuntimeError("fixture second claim failure")
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(service.worker, "_claim_pending_job", fail_second)
    service.tick(run_id)
    assert claims == 2
    assert service.get_run(run_id)["status"] == "failed"
    assert service.worker.has_live_feature_futures(run_id)
    assert service.close(timeout=0.01) is False
    with module._PROCESS_COORDINATOR.changed:
        deadline = time.monotonic() + 10
        while service.worker.has_live_feature_futures(run_id) and time.monotonic() < deadline:
            module._PROCESS_COORDINATOR.changed.wait(0.1)
    assert not service.worker.has_live_feature_futures(run_id)
    assert service.worker._feature_counts(run_id)[0] == 0
    assert service.close(timeout=10)

    resumed = StudioService(DataRoot(tmp_path), toolchain_probe=PassingToolchainProbe())
    try:
        assert resumed.retry_failed(run_id)["status"] == "pending"
        assert pump(resumed, run_id)["boundary_event"] == "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING"
        with resumed.worker.open_database(run_id) as database:
            assert database.fetchone("SELECT COUNT(*) AS n FROM jobs WHERE stage='EXTRACT_FEATURES' AND status='succeeded'")["n"] == 3
            assert database.fetchone("SELECT COUNT(*) AS n FROM audit_events WHERE event_type='FEATURE_ITEM_SUCCEEDED'")["n"] == 3
    finally:
        assert resumed.close(timeout=10)


def test_list_runs_excludes_staging_workspace(tmp_path):
    service, run_id = setup_run(tmp_path, 1, variants=1)
    try:
        hidden = service.data_root.workspace / "26.2" / ".import-staging"
        hidden.mkdir()
        (hidden / "work.sqlite3").write_bytes(b"incomplete staging database")
        assert [row["run_id"] for row in service.list_runs("26.2")] == [run_id]
    finally:
        assert service.close(timeout=10)
