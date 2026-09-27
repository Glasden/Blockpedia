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
  index_info: 'mcp-index-info-output.v2',
  search_blocks: 'mcp-search-blocks-output.v2',
  get_block_details: 'mcp-block-details-output.v2',
  compare_blocks: 'mcp-compare-blocks-output.v3',
};
const descriptions = {
  index_info: 'Read-only Blockpedia release identity and counts.',
  search_blocks: 'Read-only Blockpedia block search. Host should provide short keywords; default English canonical keywords best match the current index. Ores, containers, workstations/redstone machinery and plants rank lower unless the keywords name them. Without a colour in the query, the 16 dye colours of one block and the oxidation stages of one copper block fold into one candidate (color_series, oxidation_series). similar_to=<block_id> instead ranks blocks of the same shape class (tag shape, else full_cube, small_fixture, partial_block, sheet or passable) by top/side face colour and texture (Oklab); keywords are then optional and only narrow the pool. The first similar_to call per release reads every preview and takes a few seconds. Returns one WebP contact sheet labelled T01.. per candidate.',
  get_block_details: 'Read-only Blockpedia block details. detail="summary" (default) returns names, properties, tags, the representative state with shape class, geometry, behavior, top/side face colours and semantics, the material family (which stairs, slab, wall, fence, door, trapdoor ... exist, and the other full blocks of the same material such as log, wood, stripped log and planks), plus a 256px WebP card. detail="states" pages through every legal state (offset, limit 1-16, default 8); if release_id changes between pages, restart from offset 0.',
  compare_blocks: 'Read-only Blockpedia comparison of 2-6 blocks side by side: shape class, behavior, top/side face colours (hex, L*, texture spread, dominant colours), colour terms, materials, styles, roles and material family, plus the fields that differ. WebP contact sheet labelled T01.. in block_ids order.',
};
const blockId = { type: 'string', pattern: '^minecraft:[a-z0-9_./-]+$' };
const version = { type: 'string', pattern: '^[0-9]{1,3}\\.[0-9]{1,3}(?:\\.[0-9]{1,3})?$', minLength: 3, maxLength: 11 };
const schemaRoot = new URL('../schemas/mcp/', import.meta.url);
const outputSchema = (id) => JSON.parse(readFileSync(new URL(`${id}.json`, schemaRoot), 'utf8'));
const internalError = { error_code: 'MCP_INTERNAL_ERROR', message: 'The MCP tool failed without a safe business result.' };
const jsonSchema = new AjvJsonSchemaValidator();
const outputValidators = Object.fromEntries(Object.entries(versions).map(([name, id]) => [name, jsonSchema.getValidator(outputSchema(id))]));
const errorValidator = jsonSchema.getValidator(outputSchema('mcp-error.v2'));
if (!errorValidator(internalError).valid) throw new Error('MCP internal error schema is invalid');

function inputSchema(name) {
  const schema = { $schema: 'https://json-schema.org/draft/2020-12/schema', type: 'object', additionalProperties: false, required: [], properties: { minecraft_version: version } };
  if (name === 'search_blocks') {
    schema.properties.keywords = { type: 'array', minItems: 1, maxItems: 16, uniqueItems: true, items: { type: 'string', minLength: 1, maxLength: 64 } };
    schema.properties.limit = { type: 'integer', minimum: 1, maximum: 12, default: 8 };
    schema.properties.similar_to = { ...blockId, description: 'Rank blocks of the same shape class by palette similarity to this block; keywords become optional.' };
    // keywords or similar_to: checked by the query service.
  } else if (name === 'get_block_details') {
    schema.properties.block_id = blockId;
    schema.properties.detail = { enum: ['summary', 'states'], default: 'summary' };
    // No schema defaults: a client that fills them in would send paging to summary mode.
    schema.properties.offset = { type: 'integer', minimum: 0, description: 'detail="states" only; default 0.' };
    schema.properties.limit = { type: 'integer', minimum: 1, maximum: 16, description: 'detail="states" only; default 8.' };
    schema.required = ['block_id'];
  } else if (name === 'compare_blocks') {
    schema.properties.block_ids = { type: 'array', minItems: 2, maxItems: 6, uniqueItems: true, items: blockId };
    schema.required = ['block_ids'];
  }
  return schema;
}

const tools = names.map((name) => ({
  name,
  description: descriptions[name],
  inputSchema: inputSchema(name),
  outputSchema: { $schema: 'https://json-schema.org/draft/2020-12/schema', type: 'object', oneOf: [outputSchema(versions[name]), outputSchema('mcp-error.v2')] },
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
      for (const image of result.images ?? []) content.push({ type: 'image', data: Buffer.from(image).toString('base64'), mimeType: 'image/webp' });
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
