PRAGMA foreign_keys = ON;
PRAGMA journal_mode = DELETE;
PRAGMA synchronous = FULL;

CREATE TABLE schema_meta (
  format_version INTEGER PRIMARY KEY CHECK (format_version = 2)
);
CREATE TABLE blocks (
  block_id TEXT PRIMARY KEY,
  minecraft_version TEXT NOT NULL,
  translation_key TEXT,
  name_zh TEXT,
  name_en TEXT,
  default_state_id TEXT NOT NULL,
  machine_facts_json TEXT NOT NULL,
  record_json TEXT NOT NULL
);
CREATE TABLE states (
  state_id TEXT PRIMARY KEY,
  block_id TEXT NOT NULL REFERENCES blocks(block_id),
  properties_json TEXT NOT NULL,
  is_default INTEGER NOT NULL CHECK (is_default IN (0, 1)),
  record_json TEXT NOT NULL
);
CREATE TABLE visual_variants (
  variant_id TEXT PRIMARY KEY,
  block_id TEXT NOT NULL REFERENCES blocks(block_id),
  canonical_state_id TEXT NOT NULL REFERENCES states(state_id),
  represented_state_ids_json TEXT NOT NULL,
  preview_path TEXT NOT NULL,
  mask_path TEXT NOT NULL,
  render_metadata_path TEXT NOT NULL,
  image_sha256 TEXT NOT NULL,
  mask_sha256 TEXT NOT NULL,
  render_metadata_sha256 TEXT NOT NULL,
  candidate_qualification TEXT NOT NULL,
  warnings_json TEXT NOT NULL,
  record_json TEXT NOT NULL,
  feature_json TEXT NOT NULL
);
CREATE TABLE annotations (
  variant_id TEXT PRIMARY KEY REFERENCES visual_variants(variant_id),
  semantic_json TEXT NOT NULL
);
CREATE INDEX states_block_id_idx ON states(block_id);
CREATE INDEX visual_variants_block_id_idx ON visual_variants(block_id);
CREATE INDEX visual_variants_qualification_idx
  ON visual_variants(candidate_qualification);
