// Deterministic, read-only MCP queries over the pointer-selected release.
//
// Port of src/blockpedia/mcp_query.py onto Node 24 with the sibling
// ./release.mjs reader and ./png.mjs image composition.  Keyword recall (FTS5
// trigram or LIKE), per-request preview caching and pointer switching per
// request follow the Python business logic; ranking is the field-weighted
// scoring in ./query/search.mjs, checked against tests/r4/golden_queries.json.
// Outputs carry only what a host needs to choose blocks: no request IDs, schema
// versions, release hashes or image digests.  Release completeness stays with
// the build/activation gate; no local tamper check is reintroduced here.
import { MCPReleaseError, MCPReleaseResolver, MCPVersionInputError } from './release.mjs';
import { makeContactSheet } from './png.mjs';
import { facePalette } from './palette.mjs';
import { isMapping, fullMatch, codePointLength, normalized } from './query/text.mjs';
import {
  BLOCK_ID_RE, MCPInputError, MCPProtocolError, DETAIL_MODES, SEARCH_IMAGES,
  validateObject, validateVersionInput, validateKeywords, pageInteger, errorResult,
} from './query/input.mjs';
import { DYE_COLORS, oxidationOrder, waxedKey } from './query/blocks.mjs';
import { exactBlockQuery, parseQuery, searchIndex, rankRows } from './query/search.mjs';
import { SIMILARITY_RANGE, foldSeries, capSeries, rankSimilar } from './query/ranking.mjs';
import { IMAGE_MIME_TYPE, candidateDicts, detailsSummary, statesPage, compareData } from './query/output.mjs';

export { BLOCK_ID_RE, MCPInputError, MCPProtocolError } from './query/input.mjs';

export const OFFICIAL_DISCLAIMER =
  'NOT AN OFFICIAL MINECRAFT PRODUCT. NOT APPROVED BY OR ASSOCIATED WITH MOJANG OR MICROSOFT.';

const toolResult = (output, images = []) => ({ structuredContent: output, images: [...images], isError: false });

export class MCPQueryService {
  constructor(dataRoot) {
    this.resolver = new MCPReleaseResolver(dataRoot);
    this._snapshots = new Map();
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
    const previewPaths = {};
    let manual;
    try {
      for (const row of handle.execute('SELECT block_id, minecraft_version, default_state_id, record_json FROM blocks ORDER BY block_id')) {
        blocks[String(row.block_id)] = JSON.parse(row.record_json);
      }
      for (const row of handle.execute('SELECT state_id, block_id, record_json FROM states ORDER BY state_id')) {
        states[String(row.state_id)] = JSON.parse(row.record_json);
      }
      for (const row of handle.execute('SELECT variant_id, block_id, preview_path, record_json, feature_json FROM visual_variants ORDER BY variant_id')) {
        variants[String(row.variant_id)] = JSON.parse(row.record_json);
        features[String(row.variant_id)] = JSON.parse(row.feature_json);
        previewPaths[String(row.variant_id)] = String(row.preview_path);
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
    const snapshot = { blocks, states, variants, features, annotations, manual, previewPaths, palettes: new Map() };
    this._snapshots.set(key, snapshot);
    return snapshot;
  }

  indexInfo(argumentsValue = {}) {
    try {
      // Python defaults with `arguments or {}`, so any falsy value (null, "", 0)
      // becomes an empty object here as well.
      const args = validateObject(argumentsValue || {}, new Set(['minecraft_version']));
      const version = validateVersionInput(args);
      const handle = this.resolver.resolve(version);
      try {
        const snapshot = this._snapshot(handle);
        const builtAt = handle.release.built_at;
        if (typeof builtAt !== 'string') {
          throw new MCPReleaseError('INDEX_INFO_UNAVAILABLE', 'Release metadata needed for index_info is unavailable.', {
            minecraftVersion: handle.minecraftVersion,
          });
        }
        const skipReviews = snapshot.manual.skip_reviews;
        return toolResult({
          minecraft_version: handle.minecraftVersion,
          release_id: handle.releaseId,
          built_at: builtAt,
          counts: {
            blocks: Object.keys(snapshot.blocks).length,
            visual_variants: Object.keys(snapshot.variants).length,
            audited_skips: Array.isArray(skipReviews) ? skipReviews.length : 0,
          },
          official_disclaimer: OFFICIAL_DISCLAIMER,
        });
      } finally {
        handle.close();
      }
    } catch (error) {
      return this._mapError(error);
    }
  }

  searchBlocks(argumentsValue) {
    const resources = { previewCache: new Map() };
    try {
      const args = validateObject(argumentsValue, new Set(['minecraft_version', 'keywords', 'limit', 'similar_to', 'image']));
      const version = validateVersionInput(args);
      const similarTo = 'similar_to' in args ? args.similar_to : null;
      if ('similar_to' in args && !fullMatch(BLOCK_ID_RE, similarTo)) throw new MCPInputError('similar_to has an invalid format');
      if (!('keywords' in args) && similarTo === null) throw new MCPInputError('keywords or similar_to is required');
      const [keywords, joinedQuery] = 'keywords' in args ? validateKeywords(args.keywords) : [null, null];
      // Python's args.get("limit", 8): a present-but-null key stays null and
      // fails validation, so the default only applies when the key is absent.
      const limit = 'limit' in args ? args.limit : 8;
      if (typeof limit !== 'number' || !Number.isInteger(limit) || limit < 1 || limit > 12) {
        throw new MCPInputError('limit must be an integer from 1 to 12');
      }
      const image = 'image' in args ? args.image : 'full';
      if (!SEARCH_IMAGES.has(image)) throw new MCPInputError('image must be "full", "compact" or "none"');
      const handle = this.resolver.resolve(version);
      try {
        const snapshot = this._snapshot(handle);
        let ranked = null;
        let query = null;
        if (keywords !== null) {
          query = parseQuery(keywords, searchIndex(snapshot).lexicon);
          const exactQuery = normalized(joinedQuery);
          const recalled = this._recall(handle, this._eligibleRows(snapshot), query, exactQuery);
          ranked = rankRows(recalled, snapshot, query, exactQuery);
        }
        let selected;
        let similarity = null;
        if (similarTo === null) {
          // A waxed copper block always folds into its unwaxed twin; without a
          // colour in the query the 16 dye colours and the oxidation stages
          // of one block fold into one candidate as well.
          const unwaxed = foldSeries(ranked, 'oxidationFolded', waxedKey, oxidationOrder);
          const merged = query.colors.length > 0
            ? unwaxed
            : foldSeries(
              foldSeries(unwaxed, 'oxidationFolded', (entry) => entry.oxidation?.key ?? null, oxidationOrder),
              'colorFolded',
              (entry) => entry.series?.key ?? null,
              (entry) => DYE_COLORS.indexOf(entry.series.color),
            );
          selected = merged.slice(0, limit);
        } else {
          if (!(similarTo in snapshot.blocks)) {
            return errorResult(
              new MCPReleaseError('BLOCK_NOT_FOUND', 'The similar_to block is not in this release.', {
                minecraftVersion: handle.minecraftVersion,
              }),
              [similarTo],
            );
          }
          const similar = rankSimilar(this, handle, snapshot, similarTo, ranked, resources);
          if (similar === null) {
            return errorResult(
              new MCPReleaseError('BLOCK_HAS_NO_PREVIEW', 'The similar_to block has no preview to compare colours with.', {
                minecraftVersion: handle.minecraftVersion,
              }),
              [similarTo],
            );
          }
          selected = capSeries(foldSeries(similar.ranked, 'oxidationFolded', waxedKey, oxidationOrder)).slice(0, limit);
          // How every candidate's "ΔE …, texture …" reason reads, said once.
          similarity = {
            block_id: similarTo,
            shape_class: similar.shapeClass,
            basis: 'Same shape class only. ΔE: Oklab ×100 distance of the mean top and side face colours; '
              + `texture: 0.5 × difference in face L* spread; score = 1 − (ΔE + texture) / ${SIMILARITY_RANGE * 100}.`
              + (keywords === null ? '' : ' Keywords only narrow the pool.'),
          };
        }
        const candidates = candidateDicts(selected, snapshot);
        const output = { candidates, ...(similarity === null ? {} : { similarity }), images: [] };
        if (candidates.length === 0 || image === 'none') return toolResult(output);
        const sheet = this._contactSheet(handle, selected.map((entry) => entry.row[0]), candidates.map((item) => item.block_id), 4, resources, image);
        output.images.push(sheet.image);
        return toolResult(output, [sheet.webp]);
      } finally {
        handle.close();
      }
    } catch (error) {
      return this._mapError(error);
    }
  }

  getBlockDetails(argumentsValue) {
    const resources = { previewCache: new Map() };
    try {
      const args = validateObject(argumentsValue, new Set(['minecraft_version', 'block_id', 'detail', 'offset', 'limit']));
      const version = validateVersionInput(args);
      const blockId = args.block_id;
      if (!fullMatch(BLOCK_ID_RE, blockId)) throw new MCPInputError('block_id has an invalid format');
      const detail = 'detail' in args ? args.detail : 'summary';
      if (!DETAIL_MODES.has(detail)) throw new MCPInputError('detail must be "summary" or "states"');
      let page = null;
      if (detail === 'states') {
        page = { offset: pageInteger(args, 'offset', 0, 0, Number.MAX_SAFE_INTEGER), limit: pageInteger(args, 'limit', 8, 1, 16) };
      } else if ('offset' in args || 'limit' in args) {
        // Rejected rather than ignored: paging only applies to detail="states".
        throw new MCPInputError('offset and limit require detail="states"');
      }
      const handle = this.resolver.resolve(version);
      try {
        const snapshot = this._snapshot(handle);
        if (!(blockId in snapshot.blocks)) {
          return errorResult(
            new MCPReleaseError('BLOCK_NOT_FOUND', 'The requested block is not in this release.', {
              minecraftVersion: handle.minecraftVersion,
            }),
            [blockId],
          );
        }
        if (page !== null) return toolResult(statesPage(handle, snapshot, blockId, page));
        const [output, images] = detailsSummary(this, handle, snapshot, blockId, resources);
        return toolResult(output, images);
      } finally {
        handle.close();
      }
    } catch (error) {
      return this._mapError(error);
    }
  }

  compareBlocks(argumentsValue) {
    const resources = { previewCache: new Map() };
    try {
      const args = validateObject(argumentsValue, new Set(['minecraft_version', 'block_ids', 'image']));
      const version = validateVersionInput(args);
      const image = 'image' in args ? args.image : 'full';
      if (!SEARCH_IMAGES.has(image)) throw new MCPInputError('image must be "full", "compact" or "none"');
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
      const handle = this.resolver.resolve(version);
      try {
        const snapshot = this._snapshot(handle);
        const invalid = blockIds.filter((value) => !(value in snapshot.blocks));
        if (invalid.length > 0) {
          return errorResult(
            new MCPReleaseError('BLOCK_NOT_FOUND', 'One or more requested blocks are not in this release.', {
              minecraftVersion: handle.minecraftVersion,
            }),
            invalid,
          );
        }
        const [output, images] = compareData(this, handle, snapshot, blockIds, resources, image);
        return toolResult(output, images);
      } finally {
        handle.close();
      }
    } catch (error) {
      return this._mapError(error);
    }
  }

  callTool(name, argumentsValue = {}) {
    if (name === 'index_info') return this.indexInfo(argumentsValue);
    if (name === 'search_blocks') return this.searchBlocks(argumentsValue || {});
    if (name === 'get_block_details') return this.getBlockDetails(argumentsValue || {});
    if (name === 'compare_blocks') return this.compareBlocks(argumentsValue || {});
    throw new MCPProtocolError(-32602, 'Unknown tool name');
  }

  _mapError(error) {
    if (error instanceof MCPInputError) throw error;
    if (error instanceof MCPVersionInputError) throw new MCPInputError(error.message);
    if (error instanceof MCPReleaseError) return errorResult(error);
    throw error;
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

  // Wide recall through the release index.  A colour, shape or light intent is
  // scored from machine facts that the text index cannot see, so it recalls
  // every candidate and lets scoring decide.
  _recall(handle, rows, query, exactQuery) {
    if (query.colors.length > 0 || query.shapes.size > 0 || query.light) return [...rows];
    const texts = new Set(query.textTerms.flatMap((term) => term.matchers.map((item) => (item.han ? item.text : item.words.join(' ')))));
    const ids = new Set();
    for (const token of texts) {
      if (!token) continue;
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
    return rows.filter((row) => ids.has(row[0])
      || exactBlockQuery(exactQuery, row[1].block_id, row[3].official_names ?? {}));
  }

  // Face colours of a variant, cached with the release snapshot.  A preview
  // already decoded for this request is reused; a similarity pass over every
  // candidate reads each preview once and does not keep the decoded image.
  _palette(handle, snapshot, variantId, resources) {
    let palette = snapshot.palettes.get(variantId);
    if (palette === undefined) {
      const decoded = resources.previewCache.get(variantId)?.decoded ?? handle.readImage(snapshot.previewPaths[variantId]).decoded;
      palette = facePalette(decoded);
      snapshot.palettes.set(variantId, palette);
    }
    return palette;
  }

  // One tile per variant, row-major in candidate order.  Each tile is labelled
  // with its candidate ID and block ID ("T03 snow_block", minecraft namespace
  // dropped), so the image reads without the structured output beside it.
  _contactSheet(handle, variantIds, blockIds, columns, resources, layout = 'full') {
    const previews = variantIds.map((variantId) => this._preview(handle, variantId, resources).decoded);
    const sheet = makeContactSheet(previews, columns, layout, blockIds);
    const image = {
      content_index: 1,
      mime_type: IMAGE_MIME_TYPE,
      width: sheet.width,
      height: sheet.height,
      columns: Math.max(1, Math.min(columns, variantIds.length)),
    };
    return { image, webp: sheet.webp };
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
}

export const QueryService = MCPQueryService;
