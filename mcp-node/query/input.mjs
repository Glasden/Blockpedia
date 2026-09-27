import { isMapping, fullMatch, codePointLength, pyStrip } from './text.mjs';

export const BLOCK_ID_RE = /^minecraft:[a-z0-9_./-]+$/;
const VERSION_RE = /^[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?$/;

const ERROR_CODES = new Set([
  'DATA_ROOT_INVALID', 'CURRENT_POINTER_MISSING', 'CURRENT_POINTER_INVALID', 'VERSION_NOT_AVAILABLE',
  'RELEASE_NOT_FOUND', 'RELEASE_NOT_BUILT', 'INDEX_INFO_UNAVAILABLE', 'INDEX_OPEN_FAILED',
  'BLOCK_NOT_FOUND', 'IMAGE_READ_FAILED', 'IMAGE_MAPPING_INVALID', 'READ_ONLY_VIOLATION',
  'BLOCK_HAS_NO_PREVIEW', 'MCP_INTERNAL_ERROR',
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

export const validateVersionInput = (args) => {
  const value = args.minecraft_version;
  if (value === undefined || value === null) return null;
  if (!fullMatch(VERSION_RE, value)) throw new MCPInputError('minecraft_version has an invalid format');
  return value;
};

export const validateObject = (argumentsValue, allowed) => {
  if (!isMapping(argumentsValue) || Object.keys(argumentsValue).some((key) => typeof key !== 'string')) {
    throw new MCPInputError('tool arguments must be an object');
  }
  const value = { ...argumentsValue };
  if (Object.keys(value).some((key) => !allowed.has(key))) {
    throw new MCPInputError('tool arguments contain an unknown field');
  }
  return value;
};

// Only fields a host can act on: which versions exist, which IDs were unknown.
export const errorResult = (error, invalidBlockIds = []) => {
  const structuredContent = {
    error_code: ERROR_CODES.has(error.code) ? error.code : 'MCP_INTERNAL_ERROR',
    message: String(error.message).slice(0, 500),
  };
  if (error.availableVersions?.length) structuredContent.available_versions = [...error.availableVersions];
  if (invalidBlockIds.length) structuredContent.invalid_block_ids = [...invalidBlockIds];
  return { structuredContent, images: [], isError: true };
};

export const DETAIL_MODES = new Set(['summary', 'states']);
export const SEARCH_IMAGES = new Set(['full', 'compact', 'none']);
export const pageInteger = (args, key, fallback, min, max) => {
  const value = key in args ? args[key] : fallback;
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < min || value > max) {
    throw new MCPInputError(`${key} must be an integer from ${min} to ${max}`);
  }
  return value;
};

export function validateKeywords(value) {
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
