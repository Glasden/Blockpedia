// Deterministic, read-only MCP queries over the pointer-selected release.
//
// Port of src/blockpedia/mcp_query.py onto Node 24 with the sibling
// ./release.mjs reader and ./png.mjs contact-sheet encoder.  The four tools
// share the Python business logic exactly: keyword recall (FTS5 trigram or
// LIKE), deterministic scoring/ranking, per-request preview caching, pointer
// switching per request and the same output/error envelopes.  Release
// completeness stays with the build/activation gate; no local tamper check is
// reintroduced here.
import { createHash } from 'node:crypto';

import { MCPReleaseError, MCPReleaseResolver, MCPVersionInputError } from './release.mjs';
import { makeContactSheet, sha256Bytes } from './png.mjs';

export const BLOCK_ID_RE = /^minecraft:[a-z0-9_./-]+$/;
const VERSION_RE = /^[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?$/;
const REQUEST_ID_RE = /^[A-Za-z][A-Za-z0-9_-]{0,127}$/;

export const WEIGHTS = { shape: 0.35, color: 0.3, use: 0.15, name_synonym: 0.1, style: 0.05, behavior: 0.05 };
export const OFFICIAL_DISCLAIMER =
  'NOT AN OFFICIAL MINECRAFT PRODUCT. NOT APPROVED BY OR ASSOCIATED WITH MOJANG OR MICROSOFT.';

const COLOR_LAB_TARGETS = {
  red: [53.24, 80.09, 67.2], '红': [53.24, 80.09, 67.2], '红色': [53.24, 80.09, 67.2],
  yellow: [80.0, 0.0, 93.0], '黄': [80.0, 0.0, 93.0], '黄色': [80.0, 0.0, 93.0],
  blue: [32.3, 79.2, -107.9], '蓝': [32.3, 79.2, -107.9], '蓝色': [32.3, 79.2, -107.9],
  green: [46.2, -51.7, 49.9], '绿': [46.2, -51.7, 49.9], '绿色': [46.2, -51.7, 49.9],
  white: [100.0, 0.0, 0.0], black: [0.0, 0.0, 0.0], gray: [53.6, 0.0, 0.0], grey: [53.6, 0.0, 0.0],
};
const COLOR_OKLAB_TARGETS = {
  red: [0.628, 0.225, 0.126], '红': [0.628, 0.225, 0.126], '红色': [0.628, 0.225, 0.126],
  yellow: [0.968, -0.071, 0.199], '黄': [0.968, -0.071, 0.199], '黄色': [0.968, -0.071, 0.199],
  blue: [0.452, -0.032, -0.312], '蓝': [0.452, -0.032, -0.312], '蓝色': [0.452, -0.032, -0.312],
  green: [0.866, -0.234, 0.179], '绿': [0.866, -0.234, 0.179], '绿色': [0.866, -0.234, 0.179],
  white: [1.0, 0.0, 0.0], black: [0.0, 0.0, 0.0], gray: [0.6, 0.0, 0.0], grey: [0.6, 0.0, 0.0],
};
const COLOR_TERMS = new Set(Object.keys(COLOR_LAB_TARGETS));
const MATERIAL_TERMS = new Set(['stone', 'wood', 'brick', 'glass', 'metal', '石', '木', '砖', '玻璃']);
const USE_TERMS = new Set(['roof', 'eave', 'wall', 'floor', 'trim', '屋檐', '屋顶', '墙', '地板']);
const STYLE_TERMS = new Set(['modern', 'classic', 'simple', 'rustic', '现代', '古典', '简单']);
const SHAPE_TERMS = new Set([
  'button_like', 'cross_plane', 'fence_like', 'full_cube', 'horizontal_thin_sheet', 'irregular',
  'liquid_surface', 'pane_like', 'partial_cube', 'post_like', 'rod_like', 'slab_like', 'stair_like',
  'vertical_thin_sheet', 'wall_like', 'carpet', 'stair', 'slab', 'pane', 'wall', 'fence',
]);

const ERROR_CODES = new Set([
  'DATA_ROOT_INVALID', 'CURRENT_POINTER_MISSING', 'CURRENT_POINTER_INVALID', 'VERSION_NOT_AVAILABLE',
  'RELEASE_NOT_FOUND', 'RELEASE_NOT_BUILT', 'INDEX_INFO_UNAVAILABLE', 'INDEX_OPEN_FAILED',
  'BLOCK_NOT_FOUND', 'IMAGE_READ_FAILED', 'IMAGE_MAPPING_INVALID', 'READ_ONLY_VIOLATION',
  'MCP_INTERNAL_ERROR',
]);

export class MCPInputError extends Error {
  constructor(message) {
    super(message);
    this.name = 'MCPInputError';
    this.code = -32602;
  }
}

export class MCPProtocolError extends MCPInputError {
  constructor(code, message) {
    super(message);
    this.name = 'MCPProtocolError';
    this.code = code;
  }
}

const isMapping = (value) => typeof value === 'object' && value !== null && !Array.isArray(value);

// Python's re.fullmatch rejects a trailing newline that JS `$` would accept.
const fullMatch = (pattern, value) => {
  if (typeof value !== 'string') return false;
  const match = pattern.exec(value);
  return match !== null && match.index === 0 && match[0].length === value.length;
};

const codePointLength = (value) => [...value].length;

const byUtf8 = (left, right) => Buffer.compare(Buffer.from(left, 'utf8'), Buffer.from(right, 'utf8'));
const sortedUtf8 = (values) => [...values].sort(byUtf8);

// Python uses round(x, 8) (correctly rounded decimal).  Number(x.toFixed(8))
// parses the same decimal back to the nearest double, so they agree except on
// exact half ties, which these clamped scores do not hit.
const pyRound8 = (value) => Number(value.toFixed(8));
const clamp01 = (value) => Math.max(0.0, Math.min(1.0, value));

// Python 3.14 casefold()/str.lower()/str.isspace() semantics.
//
// Python's mcp_query._normalized is casefold() plus whitespace collapse, with no
// NFKC even though search.py's index-time normalize_text applies NFKC; matching
// that asymmetry keeps fullwidth and ligature queries behaving the same.
//
// casefold() differs from toLowerCase() at the 325 code points below (ss, st, ...,
// and the Cherokee and Beria Erfe lowerings).  The table is keyed by SOURCE code
// point, so no lowered image is ever compared: several distinct code points share
// a lower image, which a post-lower table cannot invert.  It is exactly
// {cp | casefold(cp) != toLowerCase(cp)}, so one pass over the original string is
// enough.  Python whitespace is str.isspace() over these code points, which is
// broader than JS \s (it includes U+0085, U+001C-U+001F and U+1680).
// Generated against CPython 3.14.7 and Node 24.21.0.
const CASEFOLD_OVERRIDE = new Map([
  ['\u00b5', '\u03bc'], ['\u00df', '\u0073\u0073'],
  ['\u0149', '\u02bc\u006e'], ['\u017f', '\u0073'],
  ['\u01f0', '\u006a\u030c'], ['\u0345', '\u03b9'],
  ['\u0390', '\u03b9\u0308\u0301'], ['\u03b0', '\u03c5\u0308\u0301'],
  ['\u03c2', '\u03c3'], ['\u03d0', '\u03b2'],
  ['\u03d1', '\u03b8'], ['\u03d5', '\u03c6'],
  ['\u03d6', '\u03c0'], ['\u03f0', '\u03ba'],
  ['\u03f1', '\u03c1'], ['\u03f5', '\u03b5'],
  ['\u0587', '\u0565\u0582'], ['\u13a0', '\u13a0'],
  ['\u13a1', '\u13a1'], ['\u13a2', '\u13a2'],
  ['\u13a3', '\u13a3'], ['\u13a4', '\u13a4'],
  ['\u13a5', '\u13a5'], ['\u13a6', '\u13a6'],
  ['\u13a7', '\u13a7'], ['\u13a8', '\u13a8'],
  ['\u13a9', '\u13a9'], ['\u13aa', '\u13aa'],
  ['\u13ab', '\u13ab'], ['\u13ac', '\u13ac'],
  ['\u13ad', '\u13ad'], ['\u13ae', '\u13ae'],
  ['\u13af', '\u13af'], ['\u13b0', '\u13b0'],
  ['\u13b1', '\u13b1'], ['\u13b2', '\u13b2'],
  ['\u13b3', '\u13b3'], ['\u13b4', '\u13b4'],
  ['\u13b5', '\u13b5'], ['\u13b6', '\u13b6'],
  ['\u13b7', '\u13b7'], ['\u13b8', '\u13b8'],
  ['\u13b9', '\u13b9'], ['\u13ba', '\u13ba'],
  ['\u13bb', '\u13bb'], ['\u13bc', '\u13bc'],
  ['\u13bd', '\u13bd'], ['\u13be', '\u13be'],
  ['\u13bf', '\u13bf'], ['\u13c0', '\u13c0'],
  ['\u13c1', '\u13c1'], ['\u13c2', '\u13c2'],
  ['\u13c3', '\u13c3'], ['\u13c4', '\u13c4'],
  ['\u13c5', '\u13c5'], ['\u13c6', '\u13c6'],
  ['\u13c7', '\u13c7'], ['\u13c8', '\u13c8'],
  ['\u13c9', '\u13c9'], ['\u13ca', '\u13ca'],
  ['\u13cb', '\u13cb'], ['\u13cc', '\u13cc'],
  ['\u13cd', '\u13cd'], ['\u13ce', '\u13ce'],
  ['\u13cf', '\u13cf'], ['\u13d0', '\u13d0'],
  ['\u13d1', '\u13d1'], ['\u13d2', '\u13d2'],
  ['\u13d3', '\u13d3'], ['\u13d4', '\u13d4'],
  ['\u13d5', '\u13d5'], ['\u13d6', '\u13d6'],
  ['\u13d7', '\u13d7'], ['\u13d8', '\u13d8'],
  ['\u13d9', '\u13d9'], ['\u13da', '\u13da'],
  ['\u13db', '\u13db'], ['\u13dc', '\u13dc'],
  ['\u13dd', '\u13dd'], ['\u13de', '\u13de'],
  ['\u13df', '\u13df'], ['\u13e0', '\u13e0'],
  ['\u13e1', '\u13e1'], ['\u13e2', '\u13e2'],
  ['\u13e3', '\u13e3'], ['\u13e4', '\u13e4'],
  ['\u13e5', '\u13e5'], ['\u13e6', '\u13e6'],
  ['\u13e7', '\u13e7'], ['\u13e8', '\u13e8'],
  ['\u13e9', '\u13e9'], ['\u13ea', '\u13ea'],
  ['\u13eb', '\u13eb'], ['\u13ec', '\u13ec'],
  ['\u13ed', '\u13ed'], ['\u13ee', '\u13ee'],
  ['\u13ef', '\u13ef'], ['\u13f0', '\u13f0'],
  ['\u13f1', '\u13f1'], ['\u13f2', '\u13f2'],
  ['\u13f3', '\u13f3'], ['\u13f4', '\u13f4'],
  ['\u13f5', '\u13f5'], ['\u13f8', '\u13f0'],
  ['\u13f9', '\u13f1'], ['\u13fa', '\u13f2'],
  ['\u13fb', '\u13f3'], ['\u13fc', '\u13f4'],
  ['\u13fd', '\u13f5'], ['\u1c80', '\u0432'],
  ['\u1c81', '\u0434'], ['\u1c82', '\u043e'],
  ['\u1c83', '\u0441'], ['\u1c84', '\u0442'],
  ['\u1c85', '\u0442'], ['\u1c86', '\u044a'],
  ['\u1c87', '\u0463'], ['\u1c88', '\ua64b'],
  ['\u1e96', '\u0068\u0331'], ['\u1e97', '\u0074\u0308'],
  ['\u1e98', '\u0077\u030a'], ['\u1e99', '\u0079\u030a'],
  ['\u1e9a', '\u0061\u02be'], ['\u1e9b', '\u1e61'],
  ['\u1e9e', '\u0073\u0073'], ['\u1f50', '\u03c5\u0313'],
  ['\u1f52', '\u03c5\u0313\u0300'], ['\u1f54', '\u03c5\u0313\u0301'],
  ['\u1f56', '\u03c5\u0313\u0342'], ['\u1f80', '\u1f00\u03b9'],
  ['\u1f81', '\u1f01\u03b9'], ['\u1f82', '\u1f02\u03b9'],
  ['\u1f83', '\u1f03\u03b9'], ['\u1f84', '\u1f04\u03b9'],
  ['\u1f85', '\u1f05\u03b9'], ['\u1f86', '\u1f06\u03b9'],
  ['\u1f87', '\u1f07\u03b9'], ['\u1f88', '\u1f00\u03b9'],
  ['\u1f89', '\u1f01\u03b9'], ['\u1f8a', '\u1f02\u03b9'],
  ['\u1f8b', '\u1f03\u03b9'], ['\u1f8c', '\u1f04\u03b9'],
  ['\u1f8d', '\u1f05\u03b9'], ['\u1f8e', '\u1f06\u03b9'],
  ['\u1f8f', '\u1f07\u03b9'], ['\u1f90', '\u1f20\u03b9'],
  ['\u1f91', '\u1f21\u03b9'], ['\u1f92', '\u1f22\u03b9'],
  ['\u1f93', '\u1f23\u03b9'], ['\u1f94', '\u1f24\u03b9'],
  ['\u1f95', '\u1f25\u03b9'], ['\u1f96', '\u1f26\u03b9'],
  ['\u1f97', '\u1f27\u03b9'], ['\u1f98', '\u1f20\u03b9'],
  ['\u1f99', '\u1f21\u03b9'], ['\u1f9a', '\u1f22\u03b9'],
  ['\u1f9b', '\u1f23\u03b9'], ['\u1f9c', '\u1f24\u03b9'],
  ['\u1f9d', '\u1f25\u03b9'], ['\u1f9e', '\u1f26\u03b9'],
  ['\u1f9f', '\u1f27\u03b9'], ['\u1fa0', '\u1f60\u03b9'],
  ['\u1fa1', '\u1f61\u03b9'], ['\u1fa2', '\u1f62\u03b9'],
  ['\u1fa3', '\u1f63\u03b9'], ['\u1fa4', '\u1f64\u03b9'],
  ['\u1fa5', '\u1f65\u03b9'], ['\u1fa6', '\u1f66\u03b9'],
  ['\u1fa7', '\u1f67\u03b9'], ['\u1fa8', '\u1f60\u03b9'],
  ['\u1fa9', '\u1f61\u03b9'], ['\u1faa', '\u1f62\u03b9'],
  ['\u1fab', '\u1f63\u03b9'], ['\u1fac', '\u1f64\u03b9'],
  ['\u1fad', '\u1f65\u03b9'], ['\u1fae', '\u1f66\u03b9'],
  ['\u1faf', '\u1f67\u03b9'], ['\u1fb2', '\u1f70\u03b9'],
  ['\u1fb3', '\u03b1\u03b9'], ['\u1fb4', '\u03ac\u03b9'],
  ['\u1fb6', '\u03b1\u0342'], ['\u1fb7', '\u03b1\u0342\u03b9'],
  ['\u1fbc', '\u03b1\u03b9'], ['\u1fbe', '\u03b9'],
  ['\u1fc2', '\u1f74\u03b9'], ['\u1fc3', '\u03b7\u03b9'],
  ['\u1fc4', '\u03ae\u03b9'], ['\u1fc6', '\u03b7\u0342'],
  ['\u1fc7', '\u03b7\u0342\u03b9'], ['\u1fcc', '\u03b7\u03b9'],
  ['\u1fd2', '\u03b9\u0308\u0300'], ['\u1fd3', '\u03b9\u0308\u0301'],
  ['\u1fd6', '\u03b9\u0342'], ['\u1fd7', '\u03b9\u0308\u0342'],
  ['\u1fe2', '\u03c5\u0308\u0300'], ['\u1fe3', '\u03c5\u0308\u0301'],
  ['\u1fe4', '\u03c1\u0313'], ['\u1fe6', '\u03c5\u0342'],
  ['\u1fe7', '\u03c5\u0308\u0342'], ['\u1ff2', '\u1f7c\u03b9'],
  ['\u1ff3', '\u03c9\u03b9'], ['\u1ff4', '\u03ce\u03b9'],
  ['\u1ff6', '\u03c9\u0342'], ['\u1ff7', '\u03c9\u0342\u03b9'],
  ['\u1ffc', '\u03c9\u03b9'], ['\ua7ce', '\ua7ce'],
  ['\ua7d2', '\ua7d2'], ['\ua7d4', '\ua7d4'],
  ['\uab70', '\u13a0'], ['\uab71', '\u13a1'],
  ['\uab72', '\u13a2'], ['\uab73', '\u13a3'],
  ['\uab74', '\u13a4'], ['\uab75', '\u13a5'],
  ['\uab76', '\u13a6'], ['\uab77', '\u13a7'],
  ['\uab78', '\u13a8'], ['\uab79', '\u13a9'],
  ['\uab7a', '\u13aa'], ['\uab7b', '\u13ab'],
  ['\uab7c', '\u13ac'], ['\uab7d', '\u13ad'],
  ['\uab7e', '\u13ae'], ['\uab7f', '\u13af'],
  ['\uab80', '\u13b0'], ['\uab81', '\u13b1'],
  ['\uab82', '\u13b2'], ['\uab83', '\u13b3'],
  ['\uab84', '\u13b4'], ['\uab85', '\u13b5'],
  ['\uab86', '\u13b6'], ['\uab87', '\u13b7'],
  ['\uab88', '\u13b8'], ['\uab89', '\u13b9'],
  ['\uab8a', '\u13ba'], ['\uab8b', '\u13bb'],
  ['\uab8c', '\u13bc'], ['\uab8d', '\u13bd'],
  ['\uab8e', '\u13be'], ['\uab8f', '\u13bf'],
  ['\uab90', '\u13c0'], ['\uab91', '\u13c1'],
  ['\uab92', '\u13c2'], ['\uab93', '\u13c3'],
  ['\uab94', '\u13c4'], ['\uab95', '\u13c5'],
  ['\uab96', '\u13c6'], ['\uab97', '\u13c7'],
  ['\uab98', '\u13c8'], ['\uab99', '\u13c9'],
  ['\uab9a', '\u13ca'], ['\uab9b', '\u13cb'],
  ['\uab9c', '\u13cc'], ['\uab9d', '\u13cd'],
  ['\uab9e', '\u13ce'], ['\uab9f', '\u13cf'],
  ['\uaba0', '\u13d0'], ['\uaba1', '\u13d1'],
  ['\uaba2', '\u13d2'], ['\uaba3', '\u13d3'],
  ['\uaba4', '\u13d4'], ['\uaba5', '\u13d5'],
  ['\uaba6', '\u13d6'], ['\uaba7', '\u13d7'],
  ['\uaba8', '\u13d8'], ['\uaba9', '\u13d9'],
  ['\uabaa', '\u13da'], ['\uabab', '\u13db'],
  ['\uabac', '\u13dc'], ['\uabad', '\u13dd'],
  ['\uabae', '\u13de'], ['\uabaf', '\u13df'],
  ['\uabb0', '\u13e0'], ['\uabb1', '\u13e1'],
  ['\uabb2', '\u13e2'], ['\uabb3', '\u13e3'],
  ['\uabb4', '\u13e4'], ['\uabb5', '\u13e5'],
  ['\uabb6', '\u13e6'], ['\uabb7', '\u13e7'],
  ['\uabb8', '\u13e8'], ['\uabb9', '\u13e9'],
  ['\uabba', '\u13ea'], ['\uabbb', '\u13eb'],
  ['\uabbc', '\u13ec'], ['\uabbd', '\u13ed'],
  ['\uabbe', '\u13ee'], ['\uabbf', '\u13ef'],
  ['\ufb00', '\u0066\u0066'], ['\ufb01', '\u0066\u0069'],
  ['\ufb02', '\u0066\u006c'], ['\ufb03', '\u0066\u0066\u0069'],
  ['\ufb04', '\u0066\u0066\u006c'], ['\ufb05', '\u0073\u0074'],
  ['\ufb06', '\u0073\u0074'], ['\ufb13', '\u0574\u0576'],
  ['\ufb14', '\u0574\u0565'], ['\ufb15', '\u0574\u056b'],
  ['\ufb16', '\u057e\u0576'], ['\ufb17', '\u0574\u056d'],
  ['\u{16ea0}', '\u{16ea0}'], ['\u{16ea1}', '\u{16ea1}'],
  ['\u{16ea2}', '\u{16ea2}'], ['\u{16ea3}', '\u{16ea3}'],
  ['\u{16ea4}', '\u{16ea4}'], ['\u{16ea5}', '\u{16ea5}'],
  ['\u{16ea6}', '\u{16ea6}'], ['\u{16ea7}', '\u{16ea7}'],
  ['\u{16ea8}', '\u{16ea8}'], ['\u{16ea9}', '\u{16ea9}'],
  ['\u{16eaa}', '\u{16eaa}'], ['\u{16eab}', '\u{16eab}'],
  ['\u{16eac}', '\u{16eac}'], ['\u{16ead}', '\u{16ead}'],
  ['\u{16eae}', '\u{16eae}'], ['\u{16eaf}', '\u{16eaf}'],
  ['\u{16eb0}', '\u{16eb0}'], ['\u{16eb1}', '\u{16eb1}'],
  ['\u{16eb2}', '\u{16eb2}'], ['\u{16eb3}', '\u{16eb3}'],
  ['\u{16eb4}', '\u{16eb4}'], ['\u{16eb5}', '\u{16eb5}'],
  ['\u{16eb6}', '\u{16eb6}'], ['\u{16eb7}', '\u{16eb7}'],
  ['\u{16eb8}', '\u{16eb8}'],
]);

// Python str.isspace() code points; JS \s omits 0x1c-0x1f, 0x85 and 0x1680.
const PY_WHITESPACE = new Set([
  '\u0009', '\u000a', '\u000b', '\u000c', '\u000d', '\u001c', '\u001d', '\u001e', '\u001f', '\u0020',
  '\u0085', '\u00a0', '\u1680', '\u2000', '\u2001', '\u2002', '\u2003', '\u2004', '\u2005', '\u2006',
  '\u2007', '\u2008', '\u2009', '\u200a', '\u2028', '\u2029', '\u202f', '\u205f', '\u3000',
]);

const casefold = (value) => {
  let out = '';
  for (const char of value) out += CASEFOLD_OVERRIDE.get(char) ?? char.toLowerCase();
  return out;
};

// Python str.split()/str.strip() split on str.isspace(), not JS \s.
const pySplit = (value) => {
  const parts = [];
  let current = '';
  let seen = false;
  for (const char of value) {
    if (PY_WHITESPACE.has(char)) {
      if (seen) parts.push(current);
      current = '';
      seen = false;
    } else {
      seen = true;
      current += char;
    }
  }
  if (seen) parts.push(current);
  return parts;
};

const normalized = (value) => {
  if (typeof value !== 'string') return '';
  return pySplit(casefold(value)).join(' ');
};

const pyStrip = (value) => {
  let start = 0;
  let end = value.length;
  while (start < end && PY_WHITESPACE.has(value[start])) start += 1;
  while (end > start && PY_WHITESPACE.has(value[end - 1])) end -= 1;
  return value.slice(start, end);
};

const requestId = (value, counter) => {
  if (value !== null && value !== undefined) {
    if (!fullMatch(REQUEST_ID_RE, value)) throw new MCPInputError('request_id must be an opaque identifier');
    return value;
  }
  return `mcp_${counter}`;
};

const validateVersionInput = (args) => {
  const value = args.minecraft_version;
  if (value === undefined || value === null) return null;
  if (!fullMatch(VERSION_RE, value)) throw new MCPInputError('minecraft_version has an invalid format');
  return value;
};

const validateObject = (argumentsValue, allowed) => {
  if (!isMapping(argumentsValue) || Object.keys(argumentsValue).some((key) => typeof key !== 'string')) {
    throw new MCPInputError('tool arguments must be an object');
  }
  const value = { ...argumentsValue };
  if (Object.keys(value).some((key) => !allowed.has(key))) {
    throw new MCPInputError('tool arguments contain an unknown field');
  }
  return value;
};

const errorDetails = (error, invalidBlockIds) => {
  const details = {
    release_id: error.details.release_id ?? null,
    available_versions: [...(error.availableVersions ?? [])],
    invalid_block_ids: [...invalidBlockIds],
    field_errors: [],
    provider_error_code: null,
    integrity_component: error.details.integrity_component ?? null,
  };
  for (const key of Object.keys(error.details)) if (key in details) details[key] = error.details[key];
  return details;
};

const errorResult = (error, id, invalidBlockIds = []) => {
  const structuredContent = {
    schema_version: 'mcp-error.v1',
    request_id: id,
    error_code: ERROR_CODES.has(error.code) ? error.code : 'MCP_INTERNAL_ERROR',
    message: String(error.message).slice(0, 500),
    retryable: false,
    minecraft_version: error.minecraftVersion ?? null,
    details: errorDetails(error, invalidBlockIds),
    warnings: [],
    images: [],
  };
  return { structuredContent, images: [], isError: true };
};

const toolResult = (envelope, images = []) => ({ structuredContent: envelope, images: [...images], isError: false });

const keywordTokens = (keywords) => {
  const tokens = [];
  for (const keyword of keywords) tokens.push(...normalized(keyword).split(' ').filter(Boolean));
  return tokens;
};

const keywordIntent = (keywords) => {
  const tokens = keywordTokens(keywords);
  return {
    keywords: tokens,
    colors: tokens.filter((token) => COLOR_TERMS.has(token)),
    materials: tokens.filter((token) => MATERIAL_TERMS.has(token)),
    uses: tokens.filter((token) => USE_TERMS.has(token)),
    styles: tokens.filter((token) => STYLE_TERMS.has(token)),
    shape_terms: tokens.filter((token) => SHAPE_TERMS.has(token)),
    avoid_for: [],
  };
};

const containsAny = (texts, terms) => {
  if (!terms || terms.length === 0) return 0.0;
  const haystack = texts.map((item) => normalized(item)).join(' ');
  return terms.some((term) => haystack.includes(normalized(term))) ? 1.0 : 0.0;
};

const featureColorScore = (feature, terms) => {
  const lab = feature.lab;
  const oklab = feature.oklab;
  if (!Array.isArray(lab) || lab.length !== 3 || !Array.isArray(oklab) || oklab.length !== 3) return 0.0;
  const targets = [];
  for (const term of terms) {
    const value = normalized(term);
    for (const key of Object.keys(COLOR_LAB_TARGETS)) {
      if (value.includes(key)) targets.push([COLOR_LAB_TARGETS[key], COLOR_OKLAB_TARGETS[key]]);
    }
  }
  if (targets.length === 0) return 0.0;
  let best = Infinity;
  for (const [targetLab, targetOklab] of targets) {
    let labSum = 0.0;
    let oklabSum = 0.0;
    for (let index = 0; index < 3; index += 1) {
      labSum += (Number(lab[index]) - targetLab[index]) ** 2;
      oklabSum += (Number(oklab[index]) - targetOklab[index]) ** 2;
    }
    const distance =
      (0.5 * Math.sqrt(labSum)) / 181.0 + (0.5 * Math.sqrt(oklabSum)) / Math.sqrt(3.0);
    if (distance < best) best = distance;
  }
  return pyRound8(clamp01(1.0 - best));
};

const behavior = (variant, state) => {
  const facts = variant.machine_facts;
  const byState = isMapping(facts) ? facts.behavior_by_state : undefined;
  const stateId = variant.canonical_state_id;
  const value = isMapping(byState) ? byState[stateId] : undefined;
  if (isMapping(value)) return value;
  const fallback = state.behavior;
  return isMapping(fallback) ? fallback : {};
};

const SEMANTIC_KEYS = new Set([
  'synonyms_zh', 'synonyms_en', 'summary_zh', 'summary_en', 'color_terms', 'shape_terms',
  'material_impressions', 'building_roles', 'style_tags', 'avoid_for', 'confidence',
]);
const semantic = (annotation) => {
  if (!isMapping(annotation)) return {};
  const result = {};
  for (const key of Object.keys(annotation)) if (SEMANTIC_KEYS.has(key)) result[key] = annotation[key];
  return result;
};

export const deterministicScore = (matches) => {
  const present = Object.keys(WEIGHTS).filter((key) => key in matches);
  let denominator = 0.0;
  const breakdown = {};
  for (const key of Object.keys(WEIGHTS)) {
    breakdown[key] = pyRound8(clamp01(Number(matches[key] ?? 0.0)));
  }
  for (const key of present) denominator += WEIGHTS[key];
  let score = 0.0;
  if (denominator !== 0) {
    let total = 0.0;
    for (const key of present) total += breakdown[key] * WEIGHTS[key];
    score = total / denominator;
  }
  return [pyRound8(clamp01(score)), breakdown];
};

const imageIdFor = (payload, prefix = 'img') =>
  `${prefix}_${createHash('sha256').update(payload).digest('hex').slice(0, 24)}`;

const behaviorEntries = (byState) =>
  Object.keys(byState)
    .sort(byUtf8)
    .map((stateId) => ({ state_id: stateId, behavior: byState[stateId] }));

const propertyValues = (state) =>
  Object.keys(state.properties ?? {})
    .sort(byUtf8)
    .map((name) => ({ name, value: state.properties[name] }));

export class MCPQueryService {
  constructor(dataRoot) {
    this.resolver = new MCPReleaseResolver(dataRoot);
    this._counter = 0;
    this._snapshots = new Map();
  }

  _nextRequestId(value) {
    this._counter += 1;
    return requestId(value, this._counter);
  }

  _snapshot(handle) {
    const key = `${handle.minecraftVersion}\u0000${handle.releaseId}\u0000${String(handle.releasePath)}`;
    const cached = this._snapshots.get(key);
    if (cached !== undefined) return cached;
    const blocks = {};
    const states = {};
    const variants = {};
    const features = {};
    const annotations = {};
    let manual;
    try {
      for (const row of handle.execute('SELECT block_id, minecraft_version, default_state_id, record_json FROM blocks ORDER BY block_id')) {
        blocks[String(row.block_id)] = JSON.parse(row.record_json);
      }
      for (const row of handle.execute('SELECT state_id, block_id, record_json FROM states ORDER BY state_id')) {
        states[String(row.state_id)] = JSON.parse(row.record_json);
      }
      for (const row of handle.execute('SELECT variant_id, block_id, record_json, feature_json FROM visual_variants ORDER BY variant_id')) {
        variants[String(row.variant_id)] = JSON.parse(row.record_json);
        features[String(row.variant_id)] = JSON.parse(row.feature_json);
      }
      for (const row of handle.execute('SELECT variant_id, semantic_json FROM annotations ORDER BY variant_id')) {
        const value = JSON.parse(row.semantic_json);
        if (!isMapping(value)) throw new TypeError('annotation projection mismatch');
        annotations[String(row.variant_id)] = value;
      }
      manual = JSON.parse(handle.readBytes('manual-overrides.json').toString('utf8'));
      if (!isMapping(manual)) throw new TypeError('manual record package is invalid');
    } catch (error) {
      if (error instanceof MCPReleaseError) throw error;
      throw new MCPReleaseError('INDEX_INFO_UNAVAILABLE', 'The release records could not be read.', {
        minecraftVersion: handle.minecraftVersion,
        details: { integrity_component: 'index' },
      });
    }
    const snapshot = { blocks, states, variants, features, annotations, manual };
    this._snapshots.set(key, snapshot);
    return snapshot;
  }

  indexInfo(argumentsValue = {}, options = {}) {
    const request = this._nextRequestId(options.requestId ?? null);
    try {
      // Python defaults with `arguments or {}`, so any falsy value (null, "", 0)
      // becomes an empty object here as well.
      const args = validateObject(argumentsValue || {}, new Set(['minecraft_version']));
      const version = validateVersionInput(args);
      const handle = this.resolver.resolve(version);
      try {
        const snapshot = this._snapshot(handle);
        const qualityHash = handle.manifest.quality_report_sha256;
        const builtAt = handle.release.built_at;
        if (typeof qualityHash !== 'string' || typeof builtAt !== 'string') {
          throw new MCPReleaseError('INDEX_INFO_UNAVAILABLE', 'Release metadata needed for index_info is unavailable.', {
            minecraftVersion: handle.minecraftVersion,
          });
        }
        const skipReviews = snapshot.manual.skip_reviews;
        const data = {
          product: 'Blockpedia',
          official_disclaimer: OFFICIAL_DISCLAIMER,
          release_id: handle.releaseId,
          built_at: builtAt,
          counts: {
            blocks: Object.keys(snapshot.blocks).length,
            visual_variants: Object.keys(snapshot.variants).length,
            audited_skips: Array.isArray(skipReviews) ? skipReviews.length : 0,
          },
          quality_gate: { passed: true, quality_report_sha256: qualityHash },
        };
        const envelope = {
          schema_version: 'mcp-index-info-output.v1',
          request_id: request,
          minecraft_version: handle.minecraftVersion,
          resolved_release_id: handle.releaseId,
          manifest_sha256: handle.manifestSha256,
          warnings: [],
          data,
        };
        return toolResult(envelope);
      } finally {
        handle.close();
      }
    } catch (error) {
      return this._mapError(error, request);
    }
  }

  static _validateKeywords(value) {
    if (!Array.isArray(value) || value.length < 1 || value.length > 16) {
      throw new MCPInputError('keywords must contain 1-16 strings');
    }
    const trimmed = [];
    for (const item of value) {
      if (typeof item !== 'string' || codePointLength(item) < 1 || codePointLength(item) > 64) {
        throw new MCPInputError('each keyword must contain 1-64 Unicode characters');
      }
      const value2 = pyStrip(item);
      if (codePointLength(value2) < 1 || codePointLength(value2) > 64) {
        throw new MCPInputError('each keyword must contain 1-64 non-whitespace characters');
      }
      trimmed.push(value2);
    }
    if (new Set(trimmed).size !== trimmed.length) {
      throw new MCPInputError('keywords must not contain trimmed duplicates');
    }
    return [trimmed, trimmed.join(' ')];
  }

  searchBlocks(argumentsValue, options = {}) {
    const resources = { previewCache: new Map() };
    const request = this._nextRequestId(options.requestId ?? null);
    try {
      const args = validateObject(argumentsValue, new Set(['minecraft_version', 'keywords', 'limit']));
      const version = validateVersionInput(args);
      const [keywords, joinedQuery] = MCPQueryService._validateKeywords(args.keywords);
      // Python's args.get("limit", 8): a present-but-null key stays null and
      // fails validation, so the default only applies when the key is absent.
      const limit = 'limit' in args ? args.limit : 8;
      if (typeof limit !== 'number' || !Number.isInteger(limit) || limit < 1 || limit > 12) {
        throw new MCPInputError('limit must be an integer from 1 to 12');
      }
      const intent = keywordIntent(keywords);
      const handle = this.resolver.resolve(version);
      try {
        const snapshot = this._snapshot(handle);
        const rows = this._eligibleRows(snapshot);
        const recalled = this._recall(handle, rows, intent.keywords);
        const ranked = this._rankRows(recalled, snapshot, intent);
        const selected = ranked.slice(0, 24).slice(0, limit);
        const candidates = this._candidateDicts(selected, snapshot, intent);
        const searchId = this._searchId(handle, joinedQuery);
        const exclusions = this._exclusions(snapshot, rows, recalled);
        if (selected.length === 0) {
          const data = {
            search_id: searchId,
            query: joinedQuery,
            hard_filters: [],
            exclusion_summary: exclusions,
            candidates: [],
            contact_sheet: { image_id: null, tile_mapping: [] },
            images: [],
            reranked_by_llm: false,
          };
          const envelope = {
            schema_version: 'mcp-search-blocks-output.v1',
            request_id: request,
            minecraft_version: handle.minecraftVersion,
            resolved_release_id: handle.releaseId,
            manifest_sha256: handle.manifestSha256,
            warnings: [],
            data,
          };
          return toolResult(envelope);
        }
        const sheet = this._searchSheet(handle, candidates, resources);
        const data = {
          search_id: searchId,
          query: joinedQuery,
          hard_filters: [],
          exclusion_summary: exclusions,
          candidates,
          contact_sheet: { image_id: sheet.imageId, tile_mapping: sheet.tiles },
          images: [sheet.image],
          reranked_by_llm: false,
        };
        const envelope = {
          schema_version: 'mcp-search-blocks-output.v1',
          request_id: request,
          minecraft_version: handle.minecraftVersion,
          resolved_release_id: handle.releaseId,
          manifest_sha256: handle.manifestSha256,
          warnings: [],
          data,
        };
        return toolResult(envelope, [sheet.png]);
      } finally {
        handle.close();
      }
    } catch (error) {
      return this._mapError(error, request);
    }
  }

  getBlockDetails(argumentsValue, options = {}) {
    const request = this._nextRequestId(options.requestId ?? null);
    const resources = { previewCache: new Map() };
    try {
      const args = validateObject(argumentsValue, new Set(['minecraft_version', 'block_id']));
      const version = validateVersionInput(args);
      const blockId = args.block_id;
      if (!fullMatch(BLOCK_ID_RE, blockId)) throw new MCPInputError('block_id has an invalid format');
      const handle = this.resolver.resolve(version);
      try {
        const snapshot = this._snapshot(handle);
        if (!(blockId in snapshot.blocks)) {
          return errorResult(
            new MCPReleaseError('BLOCK_NOT_FOUND', 'The requested block is not in this release.', {
              minecraftVersion: handle.minecraftVersion,
            }),
            request,
            [blockId],
          );
        }
        const [data, images] = this._detailsData(handle, snapshot, blockId, resources);
        const envelope = {
          schema_version: 'mcp-block-details-output.v1',
          request_id: request,
          minecraft_version: handle.minecraftVersion,
          resolved_release_id: handle.releaseId,
          manifest_sha256: handle.manifestSha256,
          warnings: [],
          data,
        };
        return toolResult(envelope, images);
      } finally {
        handle.close();
      }
    } catch (error) {
      return this._mapError(error, request);
    }
  }

  compareBlocks(argumentsValue, options = {}) {
    const request = this._nextRequestId(options.requestId ?? null);
    const resources = { previewCache: new Map() };
    try {
      const args = validateObject(argumentsValue, new Set(['minecraft_version', 'block_ids', 'context', 'compare_states']));
      const version = validateVersionInput(args);
      const blockIds = args.block_ids;
      if (
        !Array.isArray(blockIds)
        || blockIds.length < 2
        || blockIds.length > 6
        || blockIds.some((value) => !fullMatch(BLOCK_ID_RE, value))
        || new Set(blockIds).size !== blockIds.length
      ) {
        throw new MCPInputError('block_ids must contain 2-6 unique valid block IDs');
      }
      const context = 'context' in args ? args.context : '';
      if (typeof context !== 'string' || codePointLength(context) > 1000) {
        throw new MCPInputError('context must be a string of at most 1000 characters');
      }
      const compareStates = 'compare_states' in args ? args.compare_states : false;
      if (typeof compareStates !== 'boolean') throw new MCPInputError('compare_states must be boolean');
      const handle = this.resolver.resolve(version);
      try {
        const snapshot = this._snapshot(handle);
        const invalid = blockIds.filter((value) => !(value in snapshot.blocks));
        if (invalid.length > 0) {
          return errorResult(
            new MCPReleaseError('BLOCK_NOT_FOUND', 'One or more requested blocks are not in this release.', {
              minecraftVersion: handle.minecraftVersion,
            }),
            request,
            invalid,
          );
        }
        const [data, images] = this._compareData(handle, snapshot, blockIds, resources);
        const envelope = {
          schema_version: 'mcp-compare-blocks-output.v1',
          request_id: request,
          minecraft_version: handle.minecraftVersion,
          resolved_release_id: handle.releaseId,
          manifest_sha256: handle.manifestSha256,
          warnings: [],
          data,
        };
        return toolResult(envelope, images);
      } finally {
        handle.close();
      }
    } catch (error) {
      return this._mapError(error, request);
    }
  }

  callTool(name, argumentsValue = {}, options = {}) {
    if (name === 'index_info') return this.indexInfo(argumentsValue, options);
    if (name === 'search_blocks') return this.searchBlocks(argumentsValue || {}, options);
    if (name === 'get_block_details') return this.getBlockDetails(argumentsValue || {}, options);
    if (name === 'compare_blocks') return this.compareBlocks(argumentsValue || {}, options);
    throw new MCPProtocolError(-32602, 'Unknown tool name');
  }

  _mapError(error, request) {
    if (error instanceof MCPInputError) throw error;
    if (error instanceof MCPVersionInputError) throw new MCPInputError(error.message);
    if (error instanceof MCPReleaseError) return errorResult(error, request);
    throw error;
  }

  _searchId(handle, query) {
    return `search_${createHash('sha256').update(`${handle.manifestSha256}\u0000${query}`, 'utf8').digest('hex').slice(0, 20)}`;
  }

  _eligibleRows(snapshot) {
    const rows = [];
    for (const variantId of Object.keys(snapshot.variants)) {
      const variant = snapshot.variants[variantId];
      if (variant.candidate_qualification !== 'eligible' && variant.candidate_qualification !== 'conditional') continue;
      const state = snapshot.states[String(variant.canonical_state_id)];
      const block = snapshot.blocks[String(variant.block_id)];
      if (state !== undefined && block !== undefined) rows.push([variantId, variant, state, block]);
    }
    return rows;
  }

  _recall(handle, rows, keywords) {
    const tokens = keywordTokens(keywords);
    if (tokens.length === 0) return [...rows];
    const ids = new Set();
    for (const token of tokens) {
      let cursor;
      // Python len() counts code points; JS .length counts UTF-16 units, so a
      // supplementary character would wrongly satisfy the trigram threshold.
      if (handle.ftsMode === 'trigram' && codePointLength(token) >= 3) {
        const escaped = `"${token.replace(/"/g, ' ')}"`;
        cursor = handle.execute('SELECT variant_id FROM search_fts WHERE search_fts MATCH ?', [escaped]);
      } else if (handle.ftsMode === 'trigram') {
        cursor = handle.execute('SELECT variant_id FROM search_fts WHERE normalized_text LIKE ?', [`%${token}%`]);
      } else {
        cursor = handle.execute('SELECT variant_id FROM search_text WHERE normalized_text LIKE ?', [`%${token}%`]);
      }
      for (const row of cursor) ids.add(String(row.variant_id));
    }
    return rows.filter((row) => ids.has(row[0]));
  }

  _rankRows(rows, snapshot, intent) {
    const result = [];
    for (const row of rows) {
      const [variantId, variant, , block] = row;
      const semanticValue = semantic(snapshot.annotations[variantId]);
      const names = block.official_names ?? {};
      const feature = snapshot.features[variantId];
      const geometry = (feature.geometry_classes ?? []).map(String);
      const matches = {};
      if (intent.shape_terms.length > 0) matches.shape = containsAny(geometry, intent.shape_terms);
      if (intent.colors.length > 0) matches.color = featureColorScore(feature, intent.colors);
      if (intent.uses.length > 0) matches.use = containsAny(semanticValue.building_roles ?? [], intent.uses);
      if (intent.keywords.length > 0) {
        matches.name_synonym = containsAny(
          [names.zh_cn, names.en_us, ...(semanticValue.synonyms_zh ?? []), ...(semanticValue.synonyms_en ?? [])],
          intent.keywords,
        );
      }
      if (intent.styles.length > 0) matches.style = containsAny(semanticValue.style_tags ?? [], intent.styles);
      const [score, breakdown] = deterministicScore(matches);
      result.push([row, score, breakdown]);
    }
    result.sort((left, right) => right[1] - left[1] || byUtf8(left[0][0], right[0][0]));
    return result;
  }

  _candidateDicts(ranked, snapshot, intent) {
    const candidates = [];
    ranked.forEach((entry, index) => {
      const [row, score, breakdown] = entry;
      const [variantId, variant, state, block] = row;
      const names = block.official_names ?? {};
      const semanticValue = semantic(snapshot.annotations[variantId]);
      candidates.push({
        candidate_id: `T${String(index + 1).padStart(2, '0')}`,
        variant_id: variantId,
        block_id: String(variant.block_id),
        // Python str(names.get("zh_cn") or names.get("en_us") or variant_id):
        // an empty string falls through, so the schema minLength 1 still holds.
        display_name: String(names.zh_cn || names.en_us || variantId),
        recommended_state_id: String(variant.canonical_state_id),
        candidate_qualification: String(variant.candidate_qualification),
        local_score: score,
        final_score: score,
        score_source: 'local',
        score_breakdown: breakdown,
        reason: MCPQueryService._reason(breakdown, semanticValue),
        warnings: [...(variant.warnings ?? [])],
        machine_fact_refs: [
          { record_type: 'state', record_id: String(state.state_id), field: 'behavior' },
          { record_type: 'visual_variant', record_id: variantId, field: 'machine_facts' },
        ],
      });
    });
    return candidates;
  }

  static _reason(breakdown, semanticValue) {
    const active = Object.keys(breakdown).filter((key) => breakdown[key] > 0);
    if (active.length > 0) return `Matches ${active.join(', ')}.`;
    const summary = semanticValue.summary_en;
    return typeof summary === 'string' && summary ? summary.slice(0, 500) : 'Deterministic release candidate.';
  }

  _exclusions(snapshot, rows, recalled) {
    let excluded = 0;
    for (const variantId of Object.keys(snapshot.variants)) {
      if (snapshot.variants[variantId].candidate_qualification === 'excluded') excluded += 1;
    }
    const recallRemoved = Math.max(0, rows.length - recalled.length);
    const result = [];
    if (excluded) result.push({ reason: 'excluded qualification', count: excluded });
    if (recallRemoved) result.push({ reason: 'text recall', count: recallRemoved });
    return result;
  }

  _searchSheet(handle, candidates, resources) {
    const source = [];
    const mapping = [];
    for (const candidate of candidates) {
      const preview = this._preview(handle, candidate.variant_id, resources);
      source.push(preview);
      mapping.push({ candidate_id: candidate.candidate_id, variant_id: candidate.variant_id, block_id: candidate.block_id });
    }
    const sheet = makeContactSheet(source.map((item) => item.decoded), 4);
    const tiles = mapping.map((item, index) => ({
      candidate_id: item.candidate_id,
      variant_id: item.variant_id,
      block_id: item.block_id,
      row: Math.floor(index / 4),
      column: index % 4,
    }));
    // Python calls _image_id(png, "contact"); the second argument is the
    // ignored variant_id, so the prefix stays the default "img".
    const imageId = imageIdFor(sheet.png);
    const columns = Math.min(4, source.length);
    const image = {
      image_id: imageId,
      purpose: 'search_contact_sheet',
      mime_type: 'image/png',
      width: columns * 512,
      height: Math.ceil(source.length / columns) * 512,
      sha256: sha256Bytes(sheet.png),
      content_index: 1,
      mapping,
    };
    return { imageId, tiles, image, png: sheet.png };
  }

  _preview(handle, variantId, resources) {
    const cached = resources.previewCache.get(variantId);
    if (cached !== undefined) return cached;
    const row = handle.execute('SELECT preview_path,image_sha256 FROM visual_variants WHERE variant_id=?', [variantId])[0];
    if (row === undefined) {
      throw new MCPReleaseError('IMAGE_READ_FAILED', 'A release preview reference is missing.', {
        minecraftVersion: handle.minecraftVersion,
      });
    }
    const value = handle.readImage(String(row.preview_path));
    resources.previewCache.set(variantId, value);
    return value;
  }

  _detailsData(handle, snapshot, blockId, resources) {
    const block = snapshot.blocks[blockId];
    const states = Object.values(snapshot.states)
      .filter((state) => state.block_id === blockId)
      .sort((left, right) => byUtf8(String(left.state_id), String(right.state_id)));
    const variants = Object.values(snapshot.variants)
      .filter((variant) => variant.block_id === blockId)
      .sort((left, right) => byUtf8(String(left.variant_id), String(right.variant_id)));
    const propertyDefinitions = Object.keys(block.properties ?? {})
      .sort(byUtf8)
      .map((name) => ({ name, allowed_values: [...block.properties[name]] }));
    const images = [];
    const imageBytes = [];
    const variantOutputs = [];
    for (const variant of variants) {
      const variantId = String(variant.variant_id);
      const state = snapshot.states[String(variant.canonical_state_id)];
      if (state === undefined) {
        throw new MCPReleaseError('INDEX_INFO_UNAVAILABLE', 'A variant references unavailable state data.', {
          minecraftVersion: handle.minecraftVersion,
          details: { integrity_component: 'index' },
        });
      }
      const annotationValue = semantic(snapshot.annotations[variantId]);
      let annotation = null;
      if (
        typeof annotationValue.summary_zh === 'string'
        && typeof annotationValue.summary_en === 'string'
        && typeof annotationValue.confidence === 'number'
        && !Number.isNaN(annotationValue.confidence)
        && annotationValue.confidence >= 0
        && annotationValue.confidence <= 1
      ) {
        annotation = {
          summary_zh: annotationValue.summary_zh,
          summary_en: annotationValue.summary_en,
          confidence: annotationValue.confidence,
        };
      }
      const imageIds = [];
      if (isMapping(variant.render)) {
        const { payload, decoded } = this._preview(handle, variantId, resources);
        const imageId = imageIdFor(payload, 'img');
        imageIds.push(imageId);
        images.push({
          image_id: imageId,
          purpose: 'block_variant_views',
          mime_type: 'image/png',
          width: decoded.width,
          height: decoded.height,
          sha256: sha256Bytes(payload),
          content_index: imageBytes.length + 1,
          mapping: [{ candidate_id: null, variant_id: variantId, block_id: blockId }],
        });
        imageBytes.push(payload);
      }
      const geometry = variant.machine_facts.geometry;
      variantOutputs.push({
        variant_id: variantId,
        canonical_state_id: variant.canonical_state_id,
        represented_state_ids: [...variant.represented_state_ids],
        candidate_qualification: variant.candidate_qualification,
        warnings: [...(variant.warnings ?? [])],
        variant_facts: {
          geometry_summary: geometry.shape,
          geometry_signature: geometry.geometry_signature,
          collision_signature: geometry.collision_signature,
          geometry_classes: [...geometry.geometry_classes],
          machine_tags: [...variant.machine_facts.machine_tags],
          state_behaviors: behaviorEntries(variant.machine_facts.behavior_by_state),
        },
        annotation,
        image_ids: imageIds,
      });
    }
    const defaultState = snapshot.states[String(block.default_state_id)];
    if (defaultState === undefined) {
      throw new MCPReleaseError('INDEX_INFO_UNAVAILABLE', 'A block references unavailable default-state data.', {
        minecraftVersion: handle.minecraftVersion,
        details: { integrity_component: 'index' },
      });
    }
    const stateOutputs = states.map((state) => ({
      state_id: state.state_id,
      is_default: state.is_default,
      properties: propertyValues(state),
      shape: state.shape,
      collision: state.collision,
      behavior: state.behavior,
      variant_ids: [...state.variant_ids],
      mapping_status: state.mapping_status,
    }));
    const data = {
      block_id: blockId,
      official_names: block.official_names,
      translation_key: block.translation_key,
      default_state_id: block.default_state_id,
      property_definitions: propertyDefinitions,
      states: stateOutputs,
      block_facts: {
        has_item: block.machine_facts.has_item,
        has_block_entity: block.machine_facts.has_block_entity,
        tags: [...(block.tags ?? [])],
        default_state_behavior: defaultState.behavior,
      },
      variants: variantOutputs,
      images,
      audit: {
        skip_records: MCPQueryService._auditIds(snapshot.manual, 'skip_reviews', blockId),
        override_refs: MCPQueryService._auditIds(snapshot.manual, 'manual_overrides', blockId),
        qualification_review_refs: MCPQueryService._auditIds(snapshot.manual, 'qualification_reviews', blockId),
      },
    };
    return [data, imageBytes];
  }

  static _auditIds(manual, key, blockId) {
    const values = manual[key] ?? [];
    const result = [];
    if (!Array.isArray(values)) return result;
    for (const item of values) {
      if (!isMapping(item)) continue;
      const targetId = key === 'manual_overrides' && isMapping(item.scope) ? item.scope.variant_id : item.target_id;
      if (targetId !== blockId && item.block_id !== blockId) continue;
      // Python "a or b or c": empty strings fall through.
      const value = item.review_id || item.override_id || item.qualification_review_id;
      if (typeof value === 'string') result.push(value);
    }
    return result;
  }

  _compareData(handle, snapshot, blockIds, resources) {
    const rows = [];
    const fields = [
      ['candidate_qualification', (variant) => variant.candidate_qualification, 'machine'],
      ['geometry_classes', (variant) => (snapshot.features[String(variant.variant_id)].geometry_classes ?? []).join(','), 'machine'],
      ['transparent', (variant, state) => behavior(variant, state).transparent ?? 'unknown', 'machine'],
      ['emissive', (variant, state) => behavior(variant, state).emissive ?? 'unknown', 'machine'],
      ['emission_level', (variant, state) => behavior(variant, state).emission_level ?? 'unknown', 'machine'],
      ['redstone_related', (variant, state) => behavior(variant, state).redstone_related ?? 'unknown', 'machine'],
      ['summary_en', (variant) => semantic(snapshot.annotations[String(variant.variant_id)]).summary_en, 'annotation'],
    ];
    for (const [field, extractor, source] of fields) {
      const values = [];
      for (const blockId of blockIds) {
        const variants = Object.values(snapshot.variants)
          .filter((variant) => variant.block_id === blockId)
          .sort((left, right) => byUtf8(String(left.variant_id), String(right.variant_id)));
        if (variants.length === 0) continue;
        const variant = variants[0];
        const state = snapshot.states[String(variant.canonical_state_id)];
        if (state === undefined) continue;
        const value = extractor(variant, state);
        if (typeof value === 'boolean' || typeof value === 'number' || typeof value === 'string') {
          values.push({ block_id: blockId, value, source });
        }
      }
      const distinct = new Set(values.map((item) => scalarKey(item.value)));
      if (values.length >= 2 && distinct.size > 1) rows.push({ field, values });
    }
    const sourceImages = [];
    const mapping = [];
    blockIds.forEach((blockId, index) => {
      const variants = Object.values(snapshot.variants)
        .filter((variant) => variant.block_id === blockId)
        .sort((left, right) => byUtf8(String(left.variant_id), String(right.variant_id)));
      if (variants.length === 0) return;
      const variant = variants[0];
      sourceImages.push(this._preview(handle, String(variant.variant_id), resources));
      mapping.push({ candidate_id: `T${String(index + 1).padStart(2, '0')}`, variant_id: variant.variant_id, block_id: blockId });
    });
    if (sourceImages.length === 0) {
      return [
        { block_ids: [...blockIds], rows, contact_sheet: { image_id: null, tile_mapping: [] }, images: [] },
        [],
      ];
    }
    const sheet = makeContactSheet(sourceImages.map((item) => item.decoded), sourceImages.length);
    const tiles = mapping.map((item, index) => ({
      candidate_id: item.candidate_id,
      variant_id: item.variant_id,
      block_id: item.block_id,
      row: 0,
      column: index,
    }));
    // Matches the Python _image_id(png, "compare") call: prefix stays "img".
    const imageId = imageIdFor(sheet.png);
    const image = {
      image_id: imageId,
      purpose: 'compare_contact_sheet',
      mime_type: 'image/png',
      width: sourceImages.length * 512,
      height: 512,
      sha256: sha256Bytes(sheet.png),
      content_index: 1,
      mapping,
    };
    return [
      { block_ids: [...blockIds], rows, contact_sheet: { image_id: imageId, tile_mapping: tiles }, images: [image] },
      [sheet.png],
    ];
  }
}

const scalarKey = (value) => {
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') return JSON.stringify(value);
  return JSON.stringify(value);
};

export const QueryService = MCPQueryService;
