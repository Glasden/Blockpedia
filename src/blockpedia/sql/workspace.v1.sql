PRAGMA foreign_keys = ON;

CREATE TABLE schema_meta (
    schema_version TEXT PRIMARY KEY CHECK (schema_version = 'workspace.v1'),
    schema_sha256 TEXT NOT NULL CHECK (schema_sha256 GLOB 'sha256:[0-9a-f]*'),
    created_at TEXT NOT NULL
);

CREATE TABLE imports (
    import_id TEXT PRIMARY KEY,
    minecraft_version TEXT NOT NULL,
    export_id TEXT NOT NULL,
    source_directory_ref TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    checksum_sha256 TEXT NOT NULL,
    expected_files_json TEXT NOT NULL,
    report_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    created_at TEXT NOT NULL
);

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES imports(import_id),
    minecraft_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','running','paused','needs_review','failed','succeeded','cancelled')),
    current_stage TEXT NOT NULL,
    boundary_event TEXT,
    config_snapshot_json TEXT NOT NULL DEFAULT '{}',
    effective_config_hash TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE stage_runs (
    stage_run_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    stage TEXT NOT NULL CHECK (stage IN ('PREPARE','IMPORT_EXPORT','VALIDATE_REGISTRY','VALIDATE_VARIANTS','VALIDATE_RENDERS','EXTRACT_FEATURES','AI_ANNOTATE','VALIDATE','HUMAN_REVIEW','BUILD_RELEASE','ACTIVATE_RELEASE')),
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 10),
    status TEXT NOT NULL CHECK (status IN ('pending','running','paused','needs_review','failed','succeeded','cancelled')),
    cursor_json TEXT NOT NULL DEFAULT '{}',
    worker_id TEXT,
    recovery_attempt INTEGER NOT NULL DEFAULT 0 CHECK (recovery_attempt BETWEEN 0 AND 1),
    pause_after_item INTEGER NOT NULL DEFAULT 0 CHECK (pause_after_item IN (0,1)),
    heartbeat_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(run_id, stage),
    UNIQUE(run_id, ordinal)
);

CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    stage TEXT NOT NULL CHECK (stage IN ('PREPARE','IMPORT_EXPORT','VALIDATE_REGISTRY','VALIDATE_VARIANTS','VALIDATE_RENDERS','EXTRACT_FEATURES','AI_ANNOTATE','VALIDATE','HUMAN_REVIEW','BUILD_RELEASE','ACTIVATE_RELEASE')),
    logical_key TEXT NOT NULL,
    input_signature TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','running','succeeded','needs_review','failed','skipped')),
    auto_attempt INTEGER NOT NULL DEFAULT 0 CHECK (auto_attempt BETWEEN 0 AND 1),
    priority INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT,
    heartbeat_at TEXT,
    cursor_json TEXT NOT NULL DEFAULT '{}',
    output_hash TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(run_id, stage, logical_key, input_signature)
);

CREATE TABLE blocks (
    block_id TEXT PRIMARY KEY,
    minecraft_version TEXT NOT NULL,
    record_json TEXT NOT NULL
);

CREATE TABLE failures (
    failure_id TEXT PRIMARY KEY,
    minecraft_version TEXT NOT NULL,
    block_id TEXT REFERENCES blocks(block_id),
    state_id TEXT,
    variant_id TEXT,
    record_json TEXT NOT NULL
);

CREATE TABLE states (
    state_id TEXT PRIMARY KEY,
    block_id TEXT NOT NULL REFERENCES blocks(block_id),
    minecraft_version TEXT NOT NULL,
    record_json TEXT NOT NULL,
    failure_id TEXT REFERENCES failures(failure_id)
);

CREATE TABLE variants (
    variant_id TEXT PRIMARY KEY,
    block_id TEXT NOT NULL REFERENCES blocks(block_id),
    minecraft_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status = 'selected'),
    source_json TEXT NOT NULL,
    record_json TEXT
);

CREATE TABLE features (
    variant_id TEXT PRIMARY KEY REFERENCES variants(variant_id),
    input_sha256 TEXT NOT NULL,
    feature_extractor_version TEXT NOT NULL,
    feature_json TEXT NOT NULL,
    output_hash TEXT NOT NULL
);

CREATE TABLE review_tasks (
    review_id TEXT PRIMARY KEY,
    minecraft_version TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('normal','high')),
    status TEXT NOT NULL CHECK (status IN ('open','resolved','rejected')),
    note TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(job_id),
    kind TEXT NOT NULL,
    relative_ref TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE annotations (
    annotation_id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    minecraft_version TEXT NOT NULL,
    record_json TEXT NOT NULL
);

CREATE TABLE overrides (
    override_id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    minecraft_version TEXT NOT NULL,
    record_json TEXT NOT NULL
);

CREATE TABLE provider_profiles (
    profile_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    base_url_stable_id TEXT NOT NULL,
    secret_reference TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0,1)),
    capability_status TEXT NOT NULL DEFAULT 'unverified' CHECK (capability_status IN ('draft','unverified','verified','failed')),
    profile_json TEXT NOT NULL DEFAULT '{}',
    CHECK (active = 0 OR capability_status = 'verified')
);

CREATE TABLE provider_requests (
    request_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES provider_profiles(profile_id),
    stage TEXT NOT NULL CHECK (stage IN ('offline_annotation','query_spec','visual_rerank')),
    wire_schema_id TEXT NOT NULL CHECK (wire_schema_id IN ('annotation-batch-output.v1','query-spec-output.v1','rerank-output.v1')),
    attempt INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 2),
    cache_key TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    validated_artifact_sha256 TEXT,
    error_code TEXT,
    error_class TEXT CHECK (error_class IS NULL OR error_class IN ('retryable','non_retryable','validation','authentication','capability','unknown')),
    envelope_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','succeeded','failed','needs_review')),
    created_at TEXT NOT NULL,
    CHECK (
        (stage = 'offline_annotation' AND wire_schema_id = 'annotation-batch-output.v1') OR
        (stage = 'query_spec' AND wire_schema_id = 'query-spec-output.v1') OR
        (stage = 'visual_rerank' AND wire_schema_id = 'rerank-output.v1')
    )
);

CREATE TABLE audit_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    run_id TEXT,
    job_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE logs (
    log_id INTEGER PRIMARY KEY,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE search_documents (
    document_id TEXT PRIMARY KEY,
    block_id TEXT NOT NULL REFERENCES blocks(block_id),
    content TEXT NOT NULL,
    normalized_content TEXT NOT NULL
);

CREATE INDEX idx_stage_runs_run_ordinal ON stage_runs(run_id, ordinal);
CREATE INDEX idx_jobs_run_stage_status ON jobs(run_id, stage, status);
CREATE INDEX idx_review_tasks_status ON review_tasks(status, severity);
CREATE INDEX idx_search_documents_normalized ON search_documents(normalized_content);
CREATE UNIQUE INDEX provider_profiles_one_active ON provider_profiles(active) WHERE active = 1;
