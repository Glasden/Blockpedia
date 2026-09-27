#!/usr/bin/env node
import { readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { Worker } from 'node:worker_threads';
import { Server, ProtocolError } from '@modelcontextprotocol/server';
import { StdioServerTransport } from '@modelcontextprotocol/server/stdio';
import { AjvJsonSchemaValidator } from '@modelcontextprotocol/server/validators/ajv';

const names = ['index_info', 'search_blocks', 'get_block_details', 'compare_blocks'];
const versions = {
  index_info: 'mcp-index-info-output.v1',
  search_blocks: 'mcp-search-blocks-output.v1',
  get_block_details: 'mcp-block-details-output.v1',
  compare_blocks: 'mcp-compare-blocks-output.v1',
};
const blockId = { type: 'string', pattern: '^minecraft:[a-z0-9_./-]+$' };
const version = { type: 'string', pattern: '^[0-9]{1,3}\\.[0-9]{1,3}(?:\\.[0-9]{1,3})?$', minLength: 3, maxLength: 11 };
const schemaRoot = new URL('../schemas/mcp/', import.meta.url);
const outputSchema = (id) => JSON.parse(readFileSync(new URL(`${id}.json`, schemaRoot), 'utf8'));
const internalError = { schema_version: 'mcp-error.v1', request_id: 'mcp_transport', error_code: 'MCP_INTERNAL_ERROR', message: 'The MCP tool failed without a safe business result.', retryable: false, minecraft_version: null, details: { release_id: null, available_versions: [], invalid_block_ids: [], field_errors: [], provider_error_code: null, integrity_component: null }, warnings: [], images: [] };
const jsonSchema = new AjvJsonSchemaValidator();
const outputValidators = Object.fromEntries(Object.entries(versions).map(([name, id]) => [name, jsonSchema.getValidator(outputSchema(id))]));
const errorValidator = jsonSchema.getValidator(outputSchema('mcp-error.v1'));
if (!errorValidator(internalError).valid) throw new Error('MCP internal error schema is invalid');

function inputSchema(name) {
  const schema = { $schema: 'https://json-schema.org/draft/2020-12/schema', type: 'object', additionalProperties: false, required: [], properties: { minecraft_version: version } };
  if (name === 'search_blocks') {
    schema.properties.keywords = { type: 'array', minItems: 1, maxItems: 16, uniqueItems: true, items: { type: 'string', minLength: 1, maxLength: 64 } };
    schema.properties.limit = { type: 'integer', minimum: 1, maximum: 12, default: 8 };
    schema.required = ['keywords'];
  } else if (name === 'get_block_details') {
    schema.properties.block_id = blockId;
    schema.required = ['block_id'];
  } else if (name === 'compare_blocks') {
    schema.properties.block_ids = { type: 'array', minItems: 2, maxItems: 6, uniqueItems: true, items: blockId };
    schema.properties.context = { type: 'string', maxLength: 1000, default: '' };
    schema.properties.compare_states = { type: 'boolean', default: false };
    schema.required = ['block_ids'];
  }
  return schema;
}

const tools = names.map((name) => ({
  name,
  description: `Read-only Blockpedia ${name} query.${name === 'search_blocks' ? ' Host should provide short keywords; default English canonical keywords best match the current index.' : ''}`,
  inputSchema: inputSchema(name),
  outputSchema: { $schema: 'https://json-schema.org/draft/2020-12/schema', type: 'object', oneOf: [outputSchema(versions[name]), outputSchema('mcp-error.v1')] },
  annotations: { readOnlyHint: true },
}));

export function createServer(dataRoot, workerUrl = new URL('./query-worker.mjs', import.meta.url)) {
  const worker = new Worker(workerUrl, { workerData: { dataRoot } });
  worker.unref();
  const pending = new Map();
  let nextId = 0;
  let stopped = false;
  const failPending = () => {
    stopped = true;
    for (const { reject } of pending.values()) reject(new Error('MCP query worker stopped'));
    pending.clear();
  };
  worker.on('error', failPending);
  worker.on('exit', failPending);
  worker.on('message', ({ id, result, error }) => {
    const entry = pending.get(id);
    if (!entry) return;
    pending.delete(id);
    if (error) entry.reject(Object.assign(new Error(error.message), { code: error.code }));
    else entry.resolve(result);
  });
  const query = (name, args) => new Promise((resolve, reject) => {
    if (stopped) { reject(new Error('MCP query worker stopped')); return; }
    const id = ++nextId;
    pending.set(id, { resolve, reject });
    try { worker.postMessage({ id, name, args }); }
    catch (error) { pending.delete(id); reject(error); }
  });
  const server = new Server({ name: 'blockpedia', version: '0.0.0' }, { capabilities: { tools: {} } });
  server.onclose = () => { worker.terminate(); failPending(); };
  server.setRequestHandler('tools/list', async () => ({ tools }));
  server.setRequestHandler('tools/call', async (request) => {
    try {
      const name = request.params.name;
      const result = await query(name, request.params.arguments ?? {});
      const structuredContent = result.structuredContent;
      const valid = (result.isError ? errorValidator : outputValidators[name])(structuredContent).valid;
      if (!valid) throw new Error('MCP output schema validation failed');
      const content = [{ type: 'text', text: JSON.stringify(structuredContent) }];
      for (const image of result.images ?? []) content.push({ type: 'image', data: Buffer.from(image).toString('base64'), mimeType: 'image/png' });
      return { content, structuredContent, isError: Boolean(result.isError) };
    } catch (error) {
      if (error?.code === -32602) throw new ProtocolError(-32602, String(error.message).slice(0, 500));
      return {
        content: [{ type: 'text', text: JSON.stringify(internalError) }],
        structuredContent: internalError,
        isError: true,
      };
    }
  });
  return server;
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  const option = process.argv.indexOf('--data-root');
  if (option !== -1 && (option !== process.argv.length - 2 || !process.argv[option + 1])) {
    process.stderr.write('Usage: node server.mjs [--data-root <path>]\n');
    process.exitCode = 2;
  } else if (option === -1 && process.argv.length > 2) {
    process.stderr.write('Usage: node server.mjs [--data-root <path>]\n');
    process.exitCode = 2;
  } else {
    const dataRoot = option === -1 ? (process.env.BLOCKPEDIA_DATA_ROOT || (process.platform === 'win32' ? join(process.env.LOCALAPPDATA || join(process.env.USERPROFILE, 'AppData', 'Local'), 'Blockpedia', 'data') : join(process.env.XDG_DATA_HOME || join(process.env.HOME, '.local', 'share'), 'blockpedia'))) : process.argv[option + 1];
    const server = createServer(dataRoot);
    await server.connect(new StdioServerTransport());
  }
}
