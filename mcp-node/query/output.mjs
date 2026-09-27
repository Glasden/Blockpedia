import { MCPReleaseError } from '../release.mjs';
import { makeBlockCard } from '../png.mjs';
import { paletteOutput } from '../palette.mjs';
import { isMapping, byUtf8 } from './text.mjs';
import { blockWarnings, DYE_COLORS, materialGroups, materialFamily, materialParts, shapeKey, oxidationOrder } from './blocks.mjs';

export const IMAGE_MIME_TYPE = 'image/webp';

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
export const semantic = (annotation) => {
  if (!isMapping(annotation)) return {};
  const result = {};
  for (const key of Object.keys(annotation)) if (SEMANTIC_KEYS.has(key)) result[key] = annotation[key];
  return result;
};

const sortedObject = (mapping) => {
  const result = {};
  for (const key of Object.keys(mapping ?? {}).sort(byUtf8)) result[key] = mapping[key];
  return result;
};

// Support facts are not exported yet.  An unknown support side (and an unknown
// requires_support) is omitted rather than reported, never turned into false.
const behaviorOutput = (value) => {
  const result = {};
  if (!isMapping(value)) return result;
  for (const key of Object.keys(value)) {
    if (key === 'support') {
      const known = {};
      for (const side of Object.keys(value.support ?? {})) {
        if (value.support[side] !== 'unknown') known[side] = value.support[side];
      }
      if (Object.keys(known).length > 0) result.support = known;
    } else if (!(key === 'requires_support' && value[key] === 'unknown')) {
      result[key] = value[key];
    }
  }
  return result;
};

const boxes = (shape) => (Array.isArray(shape?.boxes) ? shape.boxes : []);
// Collision is left out when it is the same boxes as the outline shape.
const shapeOutput = (state) => {
  const shape = boxes(state.shape);
  const collision = boxes(state.collision);
  return JSON.stringify(shape) === JSON.stringify(collision) ? { shape } : { shape, collision };
};

export const variantsFor = (snapshot, blockId) =>
  Object.values(snapshot.variants)
    .filter((variant) => variant.block_id === blockId)
    .sort((left, right) => byUtf8(String(left.variant_id), String(right.variant_id)));


const round2 = (value) => Number(value.toFixed(2));

// Scores are ranked at full precision and shown to 2 decimals.  Defaults are
// left out: a recommended state equal to the block ID (a block without
// properties), eligible qualification and breakdown dimensions at 0.
export function candidateDicts(ranked, snapshot) {
  return ranked.map(({ row, score, breakdown, penalized, notes, series, colorFolded, oxidationFolded, lookSeries: look, seriesOmitted }, index) => {
    const [variantId, variant, , block] = row;
    const blockId = String(variant.block_id);
    const names = block.official_names ?? {};
    const reason = candidateReason(notes, semantic(snapshot.annotations[variantId]));
    const stateId = String(variant.canonical_state_id);
    const shown = Object.entries(breakdown).map(([key, value]) => [key, round2(value)]).filter(([, value]) => value > 0);
    return {
      candidate_id: `T${String(index + 1).padStart(2, '0')}`,
      block_id: blockId,
      // Python str(names.get("zh_cn") or names.get("en_us") or variant_id):
      // an empty string falls through, so the schema minLength 1 still holds.
      display_name: String(names.zh_cn || names.en_us || variantId),
      ...(stateId === blockId ? {} : { recommended_state_id: stateId }),
      ...(variant.candidate_qualification === 'eligible' ? {} : { candidate_qualification: String(variant.candidate_qualification) }),
      score: round2(score),
      ...(shown.length === 0 ? {} : { score_breakdown: Object.fromEntries(shown) }),
      reason: penalized ? `${reason.slice(0, 450)} Local recommendation rule: general-use score ×0.25.` : reason,
      warnings: blockWarnings(variant, blockId, block),
      ...(colorFolded === undefined ? {} : {
        color_series: {
          block_id_pattern: `minecraft:{color}_${series.key}`,
          other_colors: DYE_COLORS.filter((dye) => dye !== series.color && colorFolded.some((member) => member.series.color === dye)),
        },
      }),
      ...(oxidationFolded === undefined ? {} : {
        oxidation_series: {
          other_block_ids: [...oxidationFolded]
            .sort((left, right) => oxidationOrder(left) - oxidationOrder(right))
            .map((member) => String(member.row[1].block_id)),
        },
      }),
      ...(seriesOmitted === undefined ? {} : {
        similar_series: {
          block_id_pattern: look.pattern,
          // Words for the pattern's placeholder, not full IDs.
          omitted: seriesOmitted.map((member) => member.lookSeries.value),
        },
      }),
    };
  });
}

function candidateReason(notes, semanticValue) {
  if (notes.length > 0) {
    const text = `${notes.join('; ')}.`;
    return text.charAt(0).toUpperCase() + text.slice(1, 500);
  }
  const summary = semanticValue.summary_en;
  return typeof summary === 'string' && summary ? summary.slice(0, 500) : 'Deterministic release candidate.';
}

// A block's summary describes one representative: the canonical state of its
// first visual variant (the default state when it has none).  Geometry and the
// image belong to that state only; every legal state is read through
// detail="states".
export function detailsSummary(service, handle, snapshot, blockId, resources) {
  const block = snapshot.blocks[blockId];
  const variant = variantsFor(snapshot, blockId)[0];
  const stateId = String(variant === undefined ? block.default_state_id : variant.canonical_state_id);
  const state = snapshot.states[stateId];
  if (state === undefined) {
    throw new MCPReleaseError('INDEX_INFO_UNAVAILABLE', 'A block references unavailable state data.', {
      minecraftVersion: handle.minecraftVersion,
      details: { integrity_component: 'index' },
    });
  }
  let stateCount = 0;
  for (const value of Object.values(snapshot.states)) if (value.block_id === blockId) stateCount += 1;
  const geometry = variant?.machine_facts?.geometry ?? {};
  const warnings = blockWarnings(variant, blockId, block);
  const annotation = variant === undefined ? undefined : snapshot.annotations[String(variant.variant_id)];
  const output = {
    block_id: blockId,
    official_names: block.official_names,
    default_state_id: block.default_state_id,
    properties: sortedObject(block.properties),
    tags: [...(block.tags ?? [])],
    has_item: block.machine_facts.has_item,
    has_block_entity: block.machine_facts.has_block_entity,
    state_count: stateCount,
    representative: {
      state_id: stateId,
      candidate_qualification: variant?.candidate_qualification ?? null,
      shape_class: shapeKey(blockId, block.tags ?? [], variant?.machine_facts?.geometry),
      ...shapeOutput(state),
      geometry_classes: [...(geometry.geometry_classes ?? [])],
      machine_tags: [...(variant?.machine_facts?.machine_tags ?? [])],
      behavior: behaviorOutput(variant === undefined ? state.behavior : behavior(variant, state)),
    },
    semantics: annotation === undefined ? null : semantic(annotation),
    warnings,
    images: [],
  };
  const familyValue = family(snapshot, blockId);
  if (familyValue !== null) output.family = familyValue;
  const skip = skipReason(snapshot.manual, blockId);
  if (variant === undefined && skip !== null) output.skip_reason = skip;
  if (variant === undefined || !isMapping(variant.render)) return [output, []];
  const card = makeBlockCard(service._preview(handle, String(variant.variant_id), resources).decoded);
  const colors = paletteOutput(service._palette(handle, snapshot, String(variant.variant_id), resources));
  if (colors !== null) output.representative.colors = colors;
  output.images.push({ content_index: 1, mime_type: IMAGE_MIME_TYPE, width: card.width, height: card.height, state_id: stateId });
  return [output, [card.webp]];
}

// Real release states in UTF-8 byte order of state_id; never synthesized from
// the property cross product.  release_id lets a client restart paging when
// the published release changes between pages.
export function statesPage(handle, snapshot, blockId, { offset, limit }) {
  const states = Object.values(snapshot.states)
    .filter((state) => state.block_id === blockId)
    .sort((left, right) => byUtf8(String(left.state_id), String(right.state_id)));
  const next = offset + limit;
  return {
    block_id: blockId,
    release_id: handle.releaseId,
    total: states.length,
    offset,
    next_offset: next < states.length ? next : null,
    states: states.slice(offset, next).map((state) => ({
      state_id: state.state_id,
      is_default: state.is_default,
      properties: sortedObject(state.properties),
      ...shapeOutput(state),
      behavior: behaviorOutput(state.behavior),
      variant_ids: [...state.variant_ids],
      mapping_status: state.mapping_status,
    })),
  };
}

// The shapes of the block's material (its full block's stairs, slab ...)
// and, when the material comes in several finishes, all of those full
// blocks (spruce_log, spruce_wood, stripped_spruce_*, spruce_planks).  Null
// when the block has neither.
function family(snapshot, blockId) {
  snapshot.blockIds ??= new Set(Object.keys(snapshot.blocks));
  snapshot.materials ??= materialGroups(snapshot.blocks);
  const family = materialFamily(blockId, snapshot.blockIds);
  const base = family === null ? blockId : family.base_block;
  const group = base === null ? undefined : snapshot.materials.get(materialParts(base).root);
  const materials = group?.includes(base) ? group : undefined;
  if (family === null && materials === undefined) return null;
  // Form IDs follow one pattern per material (oak_planks: minecraft:oak_{form}),
  // so the forms are listed by name.
  const forms = Object.keys(family?.forms ?? {});
  return {
    base_block: base,
    forms,
    ...(forms.length === 0 ? {} : { form_id_pattern: `minecraft:${family.stem}_{form}` }),
    // Full blocks by template: minecraft:stripped_oak_log is stripped_*_log
    // with * = oak.
    ...(materials === undefined ? {} : {
      material: materialParts(base).core,
      material_blocks: materials.map((id) => materialParts(id).template),
    }),
  };
}

function skipReason(manual, blockId) {
  const reviews = Array.isArray(manual.skip_reviews) ? manual.skip_reviews : [];
  const review = reviews.find((item) => isMapping(item) && (item.target_id === blockId || item.block_id === blockId));
  return typeof review?.reason_code === 'string' && review.reason_code ? review.reason_code : null;
}

// Every block's comparable facts side by side, in block_ids order; each
// block describes its representative (first visual variant) like details.
// differing_fields names the fields whose values are not all equal.  A field
// equal for every block is written once under shared instead; so is a
// family or semantics key equal for every block (six planks share one forms
// list and one material_blocks list).
export function compareData(service, handle, snapshot, blockIds, resources, image = 'full') {
  const tiles = [];
  const tileBlocks = [];
  const blocks = blockIds.map((blockId) => {
    const block = snapshot.blocks[blockId];
    const names = block.official_names ?? {};
    const variant = variantsFor(snapshot, blockId)[0];
    const entry = { block_id: blockId, display_name: String(names.zh_cn || names.en_us || blockId) };
    if (variant !== undefined) {
      entry.candidate_id = `T${String(tiles.length + 1).padStart(2, '0')}`;
      tiles.push(String(variant.variant_id));
      tileBlocks.push(blockId);
    }
    const state = snapshot.states[String(variant === undefined ? block.default_state_id : variant.canonical_state_id)] ?? {};
    const facts = variant === undefined ? (isMapping(state.behavior) ? state.behavior : {}) : behavior(variant, state);
    const annotation = variant === undefined ? undefined : snapshot.annotations[String(variant.variant_id)];
    const semanticValue = semantic(annotation);
    const colors = variant === undefined || !isMapping(variant.render)
      ? null
      : paletteOutput(service._palette(handle, snapshot, String(variant.variant_id), resources));
    return {
      ...entry,
      candidate_qualification: variant?.candidate_qualification ?? null,
      shape_class: shapeKey(blockId, block.tags ?? [], variant?.machine_facts?.geometry),
      geometry_classes: [...(variant?.machine_facts?.geometry?.geometry_classes ?? [])],
      transparent: facts.transparent ?? 'unknown',
      emission_level: facts.emission_level ?? 'unknown',
      redstone_related: facts.redstone_related ?? 'unknown',
      colors,
      semantics: annotation === undefined ? null : Object.fromEntries(COMPARE_SEMANTIC_KEYS
        .filter((key) => key in semanticValue)
        .map((key) => [key, semanticValue[key]])),
      family: family(snapshot, blockId),
      warnings: blockWarnings(variant, blockId, block),
    };
  });
  const same = (values) => new Set(values.map((value) => JSON.stringify(value))).size === 1;
  const differing = COMPARE_FIELDS.filter((field) => !same(blocks.map((item) => item[field])));
  const shared = {};
  for (const field of COMPARE_FIELDS) {
    if (!differing.includes(field)) {
      shared[field] = blocks[0][field];
      for (const item of blocks) delete item[field];
    } else if (COMPARE_SHARED_KEYS.has(field) && blocks.every((item) => isMapping(item[field]))) {
      const keys = Object.keys(blocks[0][field]).filter((key) => blocks.every((item) => key in item[field]) && same(blocks.map((item) => item[field][key])));
      if (keys.length === 0) continue;
      shared[field] = Object.fromEntries(keys.map((key) => [key, blocks[0][field][key]]));
      for (const item of blocks) for (const key of keys) delete item[field][key];
    }
  }
  const output = { blocks, shared, differing_fields: differing, images: [] };
  if (tiles.length === 0 || image === 'none') return [output, []];
  const sheet = service._contactSheet(handle, tiles, tileBlocks, tiles.length, resources, image);
  output.images.push(sheet.image);
  return [output, [sheet.webp]];
}

// One summary language is enough side by side; English matches the other
// annotation terms.  Details keep both.
const COMPARE_SEMANTIC_KEYS = [
  'summary_en', 'color_terms', 'material_impressions', 'style_tags', 'building_roles',
];
// Mapping fields whose keys are shared one by one when the whole differs.
const COMPARE_SHARED_KEYS = new Set(['semantics', 'family']);
const COMPARE_FIELDS = [
  'candidate_qualification', 'shape_class', 'geometry_classes', 'transparent', 'emission_level',
  'redstone_related', 'colors', 'semantics', 'family', 'warnings',
];
