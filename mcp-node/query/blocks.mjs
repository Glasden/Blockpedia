import { byUtf8 } from './text.mjs';

const blockSet = (...ids) => new Set(ids.map((id) => `minecraft:${id}`));
const TECHNICAL_BLOCKS = blockSet(
  'air', 'cave_air', 'void_air', 'barrier', 'light', 'structure_void',
  'structure_block', 'jigsaw', 'test_block', 'test_instance_block',
  'command_block', 'chain_command_block', 'repeating_command_block',
  'moving_piston', 'piston_head', 'nether_portal', 'end_portal', 'end_gateway',
);
const INFESTED_BLOCKS = blockSet(
  'infested_stone', 'infested_cobblestone', 'infested_stone_bricks',
  'infested_mossy_stone_bricks', 'infested_cracked_stone_bricks',
  'infested_chiseled_stone_bricks', 'infested_deepslate',
);
// Structure-generated blocks that survival play cannot normally obtain or
// place; builders asking for a look-alike material want the ordinary block.
const SPECIAL_BLOCKS = blockSet(
  'bedrock', 'spawner', 'trial_spawner', 'vault', 'end_portal_frame',
  'reinforced_deepslate', 'petrified_oak_slab', 'suspicious_sand', 'suspicious_gravel',
);
const POLICY_RULES = [
  [INFESTED_BLOCKS, '本地推荐规则：虫蚀方块有蠹虫风险，泛用途检索降权；此提示不是运行时事实或人工审核。'],
  [TECHNICAL_BLOCKS, '本地推荐规则：技术或特殊用途方块在泛用途检索中降权；此提示不是运行时事实或人工审核。'],
  [SPECIAL_BLOCKS, '本地推荐规则：结构生成或生存难以获取的特殊方块，泛用途检索降权；此提示不是运行时事实或人工审核。'],
];
export const policyWarning = (blockId) => POLICY_RULES.find(([ids]) => ids.has(blockId))?.[1] ?? null;
// Behaviour warnings come from the release's registry tags first; the ID lists
// only cover behaviour that no vanilla tag expresses (minecraft:ice also holds
// packed and blue ice, which never melt).  Both sources are named in the text.
const BEHAVIOR_RULES = [
  {
    text: '受重力影响，下方悬空时会下落。',
    tags: ['sand', 'concrete_powders', 'anvil'],
    ids: blockSet('gravel', 'suspicious_gravel', 'dragon_egg'),
  },
  {
    text: '会融化：附近方块光照过强时化成水或消失，霜冰还会自行融化。',
    tags: [],
    ids: blockSet('ice', 'frosted_ice', 'snow'),
  },
  {
    text: '火会蔓延并烧毁周围可燃方块，也会自行熄灭。',
    tags: ['fire'],
    ids: blockSet(),
  },
  {
    text: '会生长或扩展（自然生长或骨粉催熟），外形和占位会改变。',
    tags: ['saplings', 'crops', 'cave_vines', 'bee_growables'],
    ids: blockSet(
      'bamboo', 'bamboo_sapling', 'sugar_cane', 'cactus', 'kelp', 'vine', 'twisting_vines',
      'weeping_vines', 'chorus_flower', 'nether_wart', 'cocoa', 'budding_amethyst',
      'red_mushroom', 'brown_mushroom',
    ),
  },
].map((rule) => ({ ...rule, tags: rule.tags.map((tag) => `minecraft:${tag}`) }));
const behaviorWarnings = (blockId, tags = []) => BEHAVIOR_RULES.flatMap((rule) => {
  const tag = rule.tags.find((value) => tags.includes(value));
  if (tag !== undefined) return [`行为提示（依据 tag ${tag}）：${rule.text}`];
  return rule.ids.has(blockId) ? [`行为提示（本地方块规则）：${rule.text}`] : [];
});
export const blockWarnings = (variant, blockId, block) => [...new Set([
  ...(variant?.warnings ?? []),
  ...(policyWarning(blockId) ? [policyWarning(blockId)] : []),
  ...behaviorWarnings(blockId, block?.tags),
])];

// Shape class of a block from its registry tags; the first matching row wins.
// Vanilla has no tag for glass panes, bars or moss carpets, so those fall back
// to the registry naming rule below until the exporter reports block classes.
const SHAPE_TAGS = [
  ['stairs', 'stairs'], ['slab', 'slabs'], ['wall', 'walls'], ['fence_gate', 'fence_gates'], ['fence', 'fences'],
  ['trapdoor', 'trapdoors'], ['door', 'doors'], ['button', 'buttons'], ['pressure_plate', 'pressure_plates'],
  ['carpet', 'wool_carpets'], ['sign', 'all_signs'], ['banner', 'banners'], ['bed', 'beds'], ['candle', 'candles'],
  ['rod', 'lightning_rods'],
].map(([shape, tag]) => [shape, `minecraft:${tag}`]);
const SHAPE_ID_SUFFIXES = [['pane', '_pane'], ['pane', '_bars'], ['carpet', '_carpet'], ['rod', '_rod'], ['chain', '_chain']];
// "wall" is ambiguous between a wall surface and the wall block shape, so it
// is not a shape intent.  In a wall block's own name (闪长岩墙) it names the
// shape and counts only as much as an annotation role; in a wall-mounted
// variant (acacia_wall_sign, white_wall_banner, wall_torch) it says where the
// item hangs and does not count at all.
export const WALL_MOUNTED_RE = /(?:^|_)wall_/;
export const WALL_WORDS = new Set(['wall', '墙']);
export const WALL_SHAPE_WEIGHT = 0.6;
// When the query names no shape, derived forms of a material rank below the
// base block, and small fixtures that only borrow its texture rank lower still.
export const SHAPE_FACTORS = new Map([
  ...['stairs', 'slab', 'wall', 'fence_gate', 'fence', 'trapdoor', 'door', 'carpet', 'pane'].map((shape) => [shape, 0.9]),
  ...['button', 'pressure_plate', 'sign', 'banner', 'wall_mounted', 'rod', 'chain'].map((shape) => [shape, 0.8]),
]);
// The 16 dye colours in creative-inventory order.  A colour series is a block
// ID suffix that all 16 carry (white_wool ... black_wool); tulips or red and
// brown mushrooms are not one.  Longer names first so light_blue_ is not read
// as blue_.
export const DYE_COLORS = [
  'white', 'light_gray', 'gray', 'black', 'brown', 'red', 'orange', 'yellow',
  'lime', 'green', 'cyan', 'light_blue', 'blue', 'purple', 'magenta', 'pink',
];
const DYE_PREFIXES = [...DYE_COLORS].sort((left, right) => right.length - left.length);
export const colorSeries = (blockId, blockIds) => {
  const path = blockId.replace(/^minecraft:/, '');
  const color = DYE_PREFIXES.find((dye) => path.startsWith(`${dye}_`));
  if (color === undefined) return null;
  const suffix = path.slice(color.length + 1);
  if (!DYE_COLORS.every((dye) => blockIds.has(`minecraft:${dye}_${suffix}`))) return null;
  return { key: suffix, color };
};
// Blocks that differ only in dye colour, wood species or coral species look
// alike by construction (the five dead coral blocks are all grey), so a
// similar_to list keeps at most SERIES_LIMIT of each series.  A wood or coral
// series is a block ID with the species word swapped that at least three
// species share (dead_{coral}_coral_block, stripped_{wood}_log).  Returns the
// series pattern and this block's word in it ({ pattern, value }), or null.
const SERIES_SPECIES = [
  ['wood', ['oak', 'spruce', 'birch', 'jungle', 'acacia', 'dark_oak', 'mangrove', 'cherry', 'pale_oak', 'bamboo', 'crimson', 'warped']],
  ['coral', ['tube', 'brain', 'bubble', 'fire', 'horn']],
].map(([kind, species]) => [kind, species, new RegExp(`(^|_)(${[...species].sort((left, right) => right.length - left.length).join('|')})(?=_|$)`)]);
export const SERIES_LIMIT = 2;
export const lookSeries = (blockId, blockIds) => {
  const dye = colorSeries(blockId, blockIds);
  if (dye !== null) return { pattern: `minecraft:{color}_${dye.key}`, value: dye.color };
  const path = blockId.replace(/^minecraft:/, '');
  for (const [kind, species, pattern] of SERIES_SPECIES) {
    const match = pattern.exec(path);
    if (match === null) continue;
    const start = match.index + match[1].length;
    const [head, tail] = [path.slice(0, start), path.slice(start + match[2].length)];
    if (species.filter((name) => blockIds.has(`minecraft:${head}${name}${tail}`)).length >= 3) {
      return { pattern: `minecraft:${head}{${kind}}${tail}`, value: match[2] };
    }
  }
  return null;
};

// The shapes a material comes in, from the registry naming rule: oak_planks
// has oak_stairs, oak_slab, oak_fence ...; stone_bricks has stone_brick_*;
// quartz_block has quartz_*; white_wool has white_carpet.  Vanilla tags name
// the shape (minecraft:stairs) but not the material, so the name decides.
// Wall-mounted signs map to their material but are not listed as a form.
const FAMILY_FORMS = [
  'stairs', 'slab', 'wall', 'fence', 'fence_gate', 'door', 'trapdoor', 'button', 'pressure_plate',
  'sign', 'hanging_sign', 'pane', 'bars', 'carpet', 'shelf',
];
const FAMILY_SUFFIXES = [...FAMILY_FORMS, 'wall_sign', 'wall_hanging_sign'].sort((left, right) => right.length - left.length);
const familyBase = (stem, blockIds) => [`${stem}_planks`, `${stem}s`, `${stem}_block`, `${stem}_wool`, stem]
  .map((path) => `minecraft:${path}`)
  .find((id) => blockIds.has(id)) ?? null;
const familyOfStem = (stem, blockIds) => {
  const forms = {};
  for (const form of FAMILY_FORMS) {
    const id = `minecraft:${stem}_${form}`;
    if (blockIds.has(id)) forms[form] = id;
  }
  return Object.keys(forms).length === 0 ? null : { base_block: familyBase(stem, blockIds), stem, forms };
};
// { base_block, forms } for a block that is a material's full block or one of
// its forms; null otherwise (bamboo_block is not the bamboo planks).
export const materialFamily = (blockId, blockIds) => {
  const path = blockId.replace(/^minecraft:/, '');
  const suffix = FAMILY_SUFFIXES.find((form) => path.endsWith(`_${form}`));
  if (suffix !== undefined) return familyOfStem(path.slice(0, -suffix.length - 1), blockIds);
  for (const stem of [path.replace(/_planks$/, ''), path.replace(/_block$/, ''), path.replace(/_wool$/, ''), path.replace(/s$/, ''), path]) {
    const family = familyOfStem(stem, blockIds);
    if (family !== null && family.base_block === blockId) return family;
  }
  return null;
};
// A material the game also offers as stairs, slab or wall is a construction
// material (stone, smooth_stone, stone_bricks, oak_planks); end_stone or a
// moss block is not.
export const hasBuildingForms = (blockId, blockIds) => {
  const family = materialFamily(blockId, blockIds);
  return family?.base_block === blockId && ['stairs', 'slab', 'wall'].some((form) => form in family.forms);
};
export const shapeClass = (blockId, tags = []) => {
  const tagged = SHAPE_TAGS.find(([, tag]) => tags.includes(tag));
  if (tagged !== undefined) return tagged[0];
  if (WALL_MOUNTED_RE.test(blockId.replace(/^minecraft:/, ''))) return 'wall_mounted';
  return SHAPE_ID_SUFFIXES.find(([, suffix]) => blockId.endsWith(suffix))?.[0] ?? null;
};
// Shape class for comparison and similarity: the tag class, else from the
// representative's geometry.  full_cube is a solid full block (tall grass and
// vines fill the block outline but have no collision); passable has no
// collision (plants, torches, rails); sheet lies flat on the floor;
// small_fixture is at most half a block across (lanterns, flower pots, heads);
// partial_block is the rest (chests, cauldrons, anvils).  other: no geometry.
export const shapeKey = (blockId, tags, geometry) => {
  const tagged = shapeClass(blockId, tags);
  if (tagged !== null) return tagged;
  const collision = geometry?.collision?.boxes;
  if (!Array.isArray(collision)) return 'other';
  if (collision.length === 0) return 'passable';
  if (geometry.is_full_cube === true) return 'full_cube';
  if ((geometry.geometry_classes ?? []).includes('horizontal_sheet')) return 'sheet';
  if (geometry.width <= 0.5 && geometry.depth <= 0.5) return 'small_fixture';
  return 'partial_block';
};

// Blocks that can share a look or a word with building materials but are not
// one: ores and raw-resource storage, containers, workstations and redstone
// machinery, and plants.  Tags first; the IDs cover what no tag expresses.
// A block with a shape class (a copper door, a moss carpet) or leaves is never
// one of these.  They rank ×0.5 unless the query asks for them.
export const NON_BUILDING_FACTOR = 0.5;
const tagSet = (...tags) => tags.map((tag) => `minecraft:${tag}`);
const CATEGORY_RULES = [
  {
    category: 'ore',
    tags: tagSet('coal_ores', 'copper_ores', 'diamond_ores', 'emerald_ores', 'gold_ores', 'iron_ores', 'lapis_ores', 'redstone_ores'),
    ids: blockSet('ancient_debris'),
    pattern: /_ore$/,
  },
  {
    category: 'resource block',
    tags: tagSet('beacon_base_blocks'),
    ids: blockSet('raw_iron_block', 'raw_copper_block', 'raw_gold_block', 'coal_block', 'lapis_block', 'redstone_block'),
  },
  {
    category: 'container',
    tags: tagSet('shulker_boxes', 'copper_chests', 'beehives'),
    ids: blockSet('chest', 'trapped_chest', 'ender_chest', 'barrel'),
  },
  {
    category: 'functional block',
    tags: tagSet('anvil', 'cauldrons', 'rails'),
    ids: blockSet(
      'crafting_table', 'furnace', 'smoker', 'blast_furnace', 'loom', 'cartography_table', 'fletching_table',
      'smithing_table', 'stonecutter', 'grindstone', 'lectern', 'enchanting_table', 'brewing_stand', 'composter',
      'jukebox', 'note_block', 'beacon', 'conduit', 'lodestone', 'respawn_anchor', 'bell', 'crafter', 'dispenser',
      'dropper', 'observer', 'piston', 'sticky_piston', 'hopper', 'daylight_detector', 'target', 'tnt',
      'sculk_sensor', 'calibrated_sculk_sensor', 'sculk_shrieker', 'sculk_catalyst', 'comparator', 'repeater',
      'redstone_wire', 'lever', 'tripwire_hook', 'redstone_torch', 'redstone_wall_torch',
    ),
    pattern: /copper_bulb$/,
  },
  {
    category: 'plant',
    tags: tagSet(
      'flowers', 'small_flowers', 'flower_pots', 'saplings', 'crops', 'bee_growables', 'corals', 'coral_plants',
      'wall_corals', 'cave_vines', 'replaceable_by_trees',
    ),
    ids: blockSet(
      'azalea', 'bamboo', 'bamboo_sapling', 'big_dripleaf', 'big_dripleaf_stem', 'small_dripleaf', 'cactus',
      'sugar_cane', 'kelp', 'kelp_plant', 'lily_pad', 'sea_pickle', 'cocoa', 'nether_wart', 'chorus_plant',
      'red_mushroom', 'brown_mushroom', 'crimson_fungus', 'warped_fungus', 'twisting_vines', 'twisting_vines_plant',
      'weeping_vines', 'weeping_vines_plant', 'pale_hanging_moss', 'attached_melon_stem', 'attached_pumpkin_stem',
    ),
    // Dead coral plants and fans carry no coral tag.
    pattern: /^minecraft:dead_[a-z]+_coral(?:_fan)?$/,
  },
];
export const blockCategory = (blockId, tags = []) => {
  if (shapeClass(blockId, tags) !== null || tags.includes('minecraft:leaves')) return null;
  return CATEGORY_RULES.find((rule) => rule.tags.some((tag) => tags.includes(tag))
    || rule.ids.has(blockId)
    || rule.pattern?.test(blockId))?.category ?? null;
};

// Copper oxidation: a copper block's four weathering stages, each also waxed,
// are one series (cut_copper_stairs ... waxed_oxidized_cut_copper_stairs).
// copper_block's stages drop the _block (exposed_copper).  A waxed block looks
// exactly like its unwaxed stage.  Null for copper that does not oxidize.
const OXIDATION_STAGES = ['', 'exposed_', 'weathered_', 'oxidized_'];
const OXIDATION_RE = /^(waxed_)?(exposed_|weathered_|oxidized_)?(.+)$/;
export const oxidationSeries = (blockId, blockIds) => {
  const [, , stage = '', rest] = OXIDATION_RE.exec(blockId.replace(/^minecraft:/, ''));
  const base = rest === 'copper' ? 'copper_block' : rest;
  const member = (wax, prefix) => `minecraft:${wax}${prefix}${prefix && base === 'copper_block' ? 'copper' : base}`;
  const members = ['', 'waxed_'].flatMap((wax) => OXIDATION_STAGES.map((prefix) => member(wax, prefix)))
    .filter((id) => blockIds.has(id));
  if (members.length < 2 || !members.includes(blockId)) return null;
  return { key: base, stage, order: members.indexOf(blockId) };
};

// Full blocks of one material in different finishes, by the registry naming
// rule: spruce_log, spruce_wood, stripped_spruce_log, stripped_spruce_wood and
// spruce_planks; tuff, polished_tuff, tuff_bricks, chiseled_tuff ...  Finish
// prefixes and block-kind suffixes are stripped to a material root; a copper
// block's wax and oxidation stay part of it (exposed_cut_copper goes with
// exposed_copper).  Forms (stairs, slab ...) stay under their full block's
// family; plants and recommendation-list blocks (infested_stone) join none.
const FINISH_PREFIXES = ['stripped_', 'polished_', 'cut_', 'chiseled_', 'cracked_', 'mossy_', 'smooth_', 'cobbled_', 'packed_'];
const KIND_SUFFIXES = ['_planks', '_log', '_wood', '_stem', '_hyphae', '_bricks', '_tiles', '_block', '_pillar', '_mosaic', '_grate'];
// root keys the group; core is the material word (oak, tuff, copper) and
// template the block's path with core as * (stripped_*_log, exposed_cut_*).
export const materialParts = (blockId) => {
  const [, waxed = '', stage = '', path] = OXIDATION_RE.exec(blockId.replace(/^minecraft:/, ''));
  let rest = path;
  let prefix;
  while ((prefix = FINISH_PREFIXES.find((value) => rest.startsWith(value))) !== undefined) rest = rest.slice(prefix.length);
  const suffix = KIND_SUFFIXES.find((value) => rest.endsWith(value)) ?? '';
  const core = rest.slice(0, rest.length - suffix.length);
  const template = `${waxed}${stage}${path.slice(0, path.length - rest.length)}*${suffix}`;
  return { root: `${waxed}${stage}${core}`, core, template };
};
// Root -> sorted full-block IDs, for roots shared by at least two blocks.
export const materialGroups = (blocks) => {
  const blockIds = new Set(Object.keys(blocks));
  const groups = new Map();
  for (const [blockId, block] of Object.entries(blocks)) {
    const family = materialFamily(blockId, blockIds);
    if (family !== null && family.base_block !== blockId) continue;
    if (policyWarning(blockId) !== null || blockCategory(blockId, block.tags ?? []) === 'plant') continue;
    const { root } = materialParts(blockId);
    groups.set(root, [...(groups.get(root) ?? []), blockId]);
  }
  return new Map([...groups].filter(([, ids]) => ids.length > 1).map(([root, ids]) => [root, ids.sort(byUtf8)]));
};

export const oxidationOrder = (entry) => entry.oxidation.order;
// A waxed block and its unwaxed stage look the same.
export const waxedKey = (entry) => (entry.oxidation ? `${entry.oxidation.key}\u0000${entry.oxidation.stage}` : null);
