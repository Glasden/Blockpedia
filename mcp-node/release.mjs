// Read-only current-pointer resolution for MCP releases.
//
// Port of src/blockpedia/mcp_release.py onto Node 24 `node:sqlite`.  Runtime
// work stays limited to pointer selection, path/reparse safety, opening the
// selected index read-only and reading the bytes a response needs.  Release
// completeness (checksums, manifest hash, schema/quality/index format) belongs
// to the WebUI build/activation gate; the cancelled local tamper checks are
// deliberately not reintroduced here.
import { lstatSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import { decodeRgbaPng } from './png.mjs';

export const MCP_VERSION_RE = /^[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?$/;
const RELEASE_ID_RE = /^rel_[0-9a-f]{32}$/;
const HASH_RE = /^sha256:[0-9a-f]{64}$/;
const TIMESTAMP_RE = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/;
const CURRENT_FIELDS = new Set(['schema_version', 'versions', 'default_minecraft_version', 'updated_at']);
const POINTER_FIELDS = new Set(['release_id', 'minecraft_version', 'relative_path', 'manifest_sha256']);
const SIDECAR_SUFFIXES = ['-wal', '-shm', '-journal', '.wal', '.shm', '.journal'];

export class MCPReleaseError extends Error {
  constructor(code, message, options = {}) {
    super(message);
    this.name = 'MCPReleaseError';
    this.code = code;
    this.minecraftVersion = options.minecraftVersion ?? null;
    this.details = { ...(options.details ?? {}) };
    this.availableVersions = [...(options.availableVersions ?? [])];
  }
}

export class MCPVersionInputError extends Error {
  constructor(message = 'minecraft_version must match the strict MCP version pattern') {
    super(message);
    this.name = 'MCPVersionInputError';
  }
}

const isMapping = (value) => typeof value === 'object' && value !== null && !Array.isArray(value);

// Python's re.fullmatch rejects a trailing newline that JS `$` would accept.
const fullMatch = (pattern, value) => {
  if (typeof value !== 'string') return false;
  const match = pattern.exec(value);
  return match !== null && match.index === 0 && match[0].length === value.length;
};

const byUtf8 = (left, right) => Buffer.compare(Buffer.from(left, 'utf8'), Buffer.from(right, 'utf8'));

const sortedKeys = (mapping) => Object.keys(mapping).map((key) => String(key)).sort(byUtf8);

const expandHome = (value) => (value.startsWith('~') ? path.join(homedir(), value.slice(1)) : value);

// Python's `_is_reparse` reads `st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT`
// (0x400), which Node's fs.Stats never exposes.  libuv's lstat only reports
// symlink/junction/LX/AppExec reparse tags as S_IFLNK, so a non-symlink reparse
// point (volume mount point, Compact OS/WOF file, cloud placeholder) is invisible
// to lstatSync and looks like a regular file or directory.  libuv's scandir sets
// UV__DT_LINK for every FILE_ATTRIBUTE_REPARSE_POINT entry, so the parent
// directory dirent reproduces the exact Python boundary with no native addon and
// no subprocess.  POSIX has no reparse points, matching Python's
// `getattr(stats, "st_file_attributes", 0) == 0`.
const isReparsePoint = (targetPath) => {
  if (process.platform !== 'win32') return false;
  const parent = path.dirname(targetPath);
  if (parent === targetPath) return false; // Volume or share root has no parent entry.
  const name = path.basename(targetPath);
  let entry;
  try {
    entry = selectDirectoryEntry(readdirSync(parent, { withFileTypes: true }), name);
  } catch {
    entry = undefined;
  }
  if (entry === undefined) {
    // Cannot prove the entry is not a reparse point: fail closed.
    throw new MCPReleaseError('CURRENT_POINTER_INVALID', 'A release path could not be inspected.');
  }
  return entry.isSymbolicLink();
};

export function selectDirectoryEntry(entries, name) {
  const exact = entries.find((candidate) => candidate.name === name);
  if (exact) return exact;
  const matches = entries.filter((candidate) => candidate.name.toLowerCase() === name.toLowerCase());
  return matches.length === 1 ? matches[0] : undefined;
}

function lstatSafe(targetPath, directory = null) {
  let stats;
  try {
    stats = lstatSync(targetPath);
  } catch {
    throw new MCPReleaseError('RELEASE_NOT_FOUND', 'The selected release is unavailable.');
  }
  if (stats.isSymbolicLink() || isReparsePoint(targetPath)) {
    throw new MCPReleaseError('CURRENT_POINTER_INVALID', 'Release links and reparse points are not allowed.');
  }
  if (directory === true && !stats.isDirectory()) {
    throw new MCPReleaseError('CURRENT_POINTER_INVALID', 'A release directory is not a directory.');
  }
  if (directory === false && !stats.isFile()) {
    throw new MCPReleaseError('CURRENT_POINTER_INVALID', 'Release files must be regular files.');
  }
  return stats;
}

function safeRelativePosixRef(value) {
  if (typeof value !== 'string' || value.length === 0 || value.includes('\\') || value.includes('\0')) {
    throw new Error('unsafe reference');
  }
  if (value.startsWith('/') || /^[A-Za-z]:/.test(value)) throw new Error('unsafe reference');
  if (value.split('/').some((part) => part === '' || part === '.' || part === '..')) throw new Error('unsafe reference');
  if (path.posix.isAbsolute(value)) throw new Error('unsafe reference');
  return value;
}

function insideRoot(root, candidate) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return relative === '' || (!relative.startsWith('..') && !path.isAbsolute(relative));
}

function safeComponents(targetPath, root, finalDirectory = null) {
  if (!insideRoot(root, targetPath)) {
    throw new MCPReleaseError('CURRENT_POINTER_INVALID', 'A release path escapes its data root.');
  }
  lstatSafe(root, true);
  const parts = path.relative(path.resolve(root), path.resolve(targetPath)).split(path.sep).filter((part) => part !== '');
  let current = root;
  for (let index = 0; index < parts.length; index += 1) {
    current = path.join(current, parts[index]);
    lstatSafe(current, index === parts.length - 1 ? finalDirectory : null);
  }
}

function safeChild(root, relativeRef, directory = null) {
  try {
    safeRelativePosixRef(relativeRef);
  } catch {
    throw new MCPReleaseError('CURRENT_POINTER_INVALID', 'A release path escapes its data root.');
  }
  const candidate = path.join(root, ...relativeRef.split('/'));
  if (!insideRoot(root, candidate)) {
    throw new MCPReleaseError('CURRENT_POINTER_INVALID', 'A release path escapes its data root.');
  }
  safeComponents(candidate, root, directory);
  return candidate;
}

function readRegular(targetPath, root) {
  safeComponents(targetPath, root, false);
  try {
    return readFileSync(targetPath);
  } catch {
    throw new MCPReleaseError('RELEASE_NOT_FOUND', 'A release file could not be read.');
  }
}

function jsonFile(filePath, root, component) {
  const code = component === 'current_pointer' ? 'CURRENT_POINTER_INVALID' : 'INDEX_INFO_UNAVAILABLE';
  let value;
  try {
    value = JSON.parse(readRegular(filePath, root).toString('utf8'));
  } catch (error) {
    if (error instanceof MCPReleaseError) throw error;
    throw new MCPReleaseError(code, 'A required release JSON file is unavailable or malformed.', {
      details: { integrity_component: component },
    });
  }
  if (!isMapping(value)) {
    throw new MCPReleaseError(code, 'A required release JSON file is not an object.', {
      details: { integrity_component: component },
    });
  }
  return value;
}

const URI_SAFE_CHAR = /[A-Za-z0-9/:.~_\\-]/;

function quotePath(value) {
  let out = '';
  for (const char of value) {
    if (URI_SAFE_CHAR.test(char)) {
      out += char;
      continue;
    }
    for (const byte of Buffer.from(char, 'utf8')) out += `%${byte.toString(16).toUpperCase().padStart(2, '0')}`;
  }
  return out;
}

const immutableUri = (filePath) => `file:${quotePath(filePath.split(path.sep).join('/'))}?mode=ro&immutable=1`;

function rejectSidecar(name) {
  if (SIDECAR_SUFFIXES.some((suffix) => name.endsWith(suffix)) || name.startsWith('index.sqlite3-')) {
    throw new MCPReleaseError('INDEX_OPEN_FAILED', 'SQLite sidecar files are not allowed.');
  }
}

function openIndex(indexPath, minecraftVersion) {
  let connection;
  try {
    safeComponents(indexPath, path.dirname(indexPath), false);
    rejectSidecar(path.basename(indexPath));
    connection = new DatabaseSync(immutableUri(indexPath), { readOnly: true });
    connection.exec('PRAGMA query_only=ON');
    return connection;
  } catch (error) {
    if (connection !== undefined) connection.close();
    if (error instanceof MCPReleaseError && (error.code === 'CURRENT_POINTER_INVALID' || error.code === 'INDEX_OPEN_FAILED')) throw error;
    throw new MCPReleaseError('INDEX_OPEN_FAILED', 'The pointer-selected release index could not be opened.', {
      minecraftVersion,
      details: { integrity_component: 'index' },
    });
  }
}

export class ReleaseHandle {
  constructor({ dataRoot, minecraftVersion, releaseId, releasePath, release, manifest, manifestSha256, connection }) {
    this.dataRoot = dataRoot;
    this.minecraftVersion = minecraftVersion;
    this.releaseId = releaseId;
    this.releasePath = releasePath;
    this.release = release;
    this.manifest = manifest;
    this.manifestSha256 = manifestSha256;
    this.connection = connection;
  }

  get ftsMode() {
    const value = this.manifest.fts_mode ?? 'trigram';
    if (value !== 'trigram' && value !== 'normalized_like') {
      throw new MCPReleaseError('INDEX_INFO_UNAVAILABLE', 'The release search mode declaration is unavailable.', {
        minecraftVersion: this.minecraftVersion,
      });
    }
    return value;
  }

  execute(statement, parameters = []) {
    try {
      return this.connection.prepare(statement).all(...parameters);
    } catch {
      throw new MCPReleaseError('INDEX_INFO_UNAVAILABLE', 'The release index could not be read.', {
        minecraftVersion: this.minecraftVersion,
        details: { integrity_component: 'index' },
      });
    }
  }

  readBytes(relativeRef) {
    try {
      return readRegular(safeChild(this.releasePath, relativeRef, false), this.releasePath);
    } catch (error) {
      if (error instanceof MCPReleaseError) throw error;
      throw new MCPReleaseError('RELEASE_NOT_FOUND', 'The requested release file is unavailable.', {
        minecraftVersion: this.minecraftVersion,
      });
    }
  }

  readImage(relativeRef) {
    let payload;
    let decoded;
    try {
      payload = this.readBytes(relativeRef);
      decoded = decodeRgbaPng(payload);
    } catch (error) {
      if (error instanceof MCPReleaseError && error.code === 'IMAGE_READ_FAILED') throw error;
      throw new MCPReleaseError('IMAGE_READ_FAILED', 'A release image could not be read.', {
        minecraftVersion: this.minecraftVersion,
      });
    }
    return { payload, decoded };
  }

  close() {
    this.connection.close();
  }

  [Symbol.dispose]() {
    this.close();
  }
}

export class MCPReleaseResolver {
  constructor(dataRoot) {
    this.dataRoot = path.resolve(expandHome(String(dataRoot)));
  }

  get currentPath() {
    return path.join(this.dataRoot, 'current.json');
  }

  readCurrent() {
    let available = false;
    try {
      available = statSync(this.dataRoot).isDirectory();
    } catch {
      available = false;
    }
    if (!available) throw new MCPReleaseError('DATA_ROOT_INVALID', 'The configured data root is unavailable.');
    try {
      return jsonFile(this.currentPath, this.dataRoot, 'current_pointer');
    } catch (error) {
      if (error instanceof MCPReleaseError && error.code === 'RELEASE_NOT_FOUND') {
        throw new MCPReleaseError('CURRENT_POINTER_MISSING', 'The current release pointer is missing.');
      }
      throw new MCPReleaseError('CURRENT_POINTER_INVALID', 'The current release pointer is invalid.');
    }
  }

  availableVersions() {
    let current;
    try {
      current = this.readCurrent();
    } catch (error) {
      if (error instanceof MCPReleaseError) return [];
      throw error;
    }
    return isMapping(current.versions) ? sortedKeys(current.versions) : [];
  }

  resolve(minecraftVersion) {
    if (minecraftVersion != null && !fullMatch(MCP_VERSION_RE, minecraftVersion)) {
      throw new MCPVersionInputError();
    }
    const current = this.readCurrent();
    const keys = Object.keys(current);
    if (
      keys.length !== CURRENT_FIELDS.size
      || !keys.every((key) => CURRENT_FIELDS.has(key))
      || current.schema_version !== 'current-pointer.v1'
    ) {
      throw new MCPReleaseError('CURRENT_POINTER_INVALID', 'The current release pointer is invalid.');
    }
    const versions = current.versions;
    const fallback = current.default_minecraft_version;
    if (
      !isMapping(versions)
      || Object.keys(versions).some((key) => !fullMatch(MCP_VERSION_RE, key))
      || typeof fallback !== 'string'
      || !fullMatch(MCP_VERSION_RE, fallback)
      || !Object.hasOwn(versions, fallback)
    ) {
      throw new MCPReleaseError('CURRENT_POINTER_INVALID', 'The current release pointer has no valid default version.');
    }
    if (!fullMatch(TIMESTAMP_RE, current.updated_at)) {
      throw new MCPReleaseError('CURRENT_POINTER_INVALID', 'The current release pointer timestamp is invalid.');
    }
    const selected = minecraftVersion == null ? fallback : minecraftVersion;
    if (!Object.hasOwn(versions, selected)) {
      throw new MCPReleaseError('VERSION_NOT_AVAILABLE', 'The requested Minecraft version is not published.', {
        minecraftVersion: selected,
        availableVersions: sortedKeys(versions),
      });
    }
    const pointer = versions[selected];
    this.checkPointerInvariants(selected, pointer);
    const releaseId = String(pointer.release_id);
    const releasePath = safeChild(this.dataRoot, String(pointer.relative_path), true);
    if (path.basename(releasePath) !== releaseId) {
      throw new MCPReleaseError('CURRENT_POINTER_INVALID', 'The current pointer release path is not exact.', {
        minecraftVersion: selected,
      });
    }
    return this.loadRelease(selected, releaseId, releasePath, String(pointer.manifest_sha256));
  }

  checkPointerInvariants(selected, pointer) {
    const fail = (message) => {
      throw new MCPReleaseError('CURRENT_POINTER_INVALID', message, { minecraftVersion: selected });
    };
    const keys = pointer !== null && typeof pointer === 'object' && !Array.isArray(pointer) ? Object.keys(pointer) : null;
    if (keys === null || keys.length !== POINTER_FIELDS.size || !keys.every((key) => POINTER_FIELDS.has(key))) {
      fail('The selected current pointer is invalid.');
    }
    const releaseId = pointer.release_id;
    if (!fullMatch(RELEASE_ID_RE, releaseId)) fail('The current pointer release ID is invalid.');
    if (pointer.minecraft_version !== selected) fail('The current pointer version does not match its key.');
    if (pointer.relative_path !== `releases/${selected}/${releaseId}`) fail('The current pointer path is invalid.');
    if (!fullMatch(HASH_RE, pointer.manifest_sha256)) {
      fail('The current pointer manifest declaration is invalid.');
    }
  }

  loadRelease(version, releaseId, releasePath, pointerManifestHash) {
    let release;
    let manifest;
    let connection;
    try {
      release = jsonFile(path.join(releasePath, 'release.json'), releasePath, 'release');
      manifest = jsonFile(path.join(releasePath, 'manifest.json'), releasePath, 'manifest');
      connection = openIndex(path.join(releasePath, 'index.sqlite3'), version);
    } catch (error) {
      if (connection !== undefined) connection.close();
      if (error instanceof MCPReleaseError) throw error;
      throw new MCPReleaseError('INDEX_OPEN_FAILED', 'The pointer-selected release could not be opened.', {
        minecraftVersion: version,
        details: { integrity_component: 'index' },
      });
    }
    return new ReleaseHandle({
      dataRoot: this.dataRoot,
      minecraftVersion: version,
      releaseId,
      releasePath,
      release,
      manifest,
      manifestSha256: pointerManifestHash,
      connection,
    });
  }
}

export const ReleaseResolver = MCPReleaseResolver;
