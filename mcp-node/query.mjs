// Deterministic, read-only MCP queries over the pointer-selected release.
//
// Port of src/blockpedia/mcp_query.py onto Node 24 with the sibling
// ./release.mjs reader and ./png.mjs image composition.  Keyword recall (FTS5
// trigram or LIKE), per-request preview caching and pointer switching per
// request follow the Python business logic; ranking is the field-weighted
// scoring below, checked against tests/r4/golden_queries.json.  Outputs
// carry only what a host needs to choose blocks: no request IDs, schema
// versions, release hashes or image digests.  Release completeness stays with
// the build/activation gate; no local tamper check is reintroduced here.
import { MCPReleaseError, MCPReleaseResolver, MCPVersionInputError } from './release.mjs';
import { makeBlockCard, makeContactSheet } from './png.mjs';
import { facePalette, paletteDistance, paletteOutput } from './palette.mjs';

export const BLOCK_ID_RE = /^minecraft:[a-z0-9_./-]+$/;
const VERSION_RE = /^[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?$/;
const IMAGE_MIME_TYPE = 'image/webp';

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
const policyWarning = (blockId) => POLICY_RULES.find(([ids]) => ids.has(blockId))?.[1] ?? null;
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
const blockWarnings = (variant, blockId, block) => [...new Set([
  ...(variant?.warnings ?? []),
  ...(policyWarning(blockId) ? [policyWarning(blockId)] : []),
  ...behaviorWarnings(blockId, block?.tags),
])];
const exactBlockQuery = (query, blockId, names) =>
  query === normalized(blockId)
  || [names.zh_cn, names.en_us].some((name) => name && query === normalized(name));
export const OFFICIAL_DISCLAIMER =
  'NOT AN OFFICIAL MINECRAFT PRODUCT. NOT APPROVED BY OR ASSOCIATED WITH MOJANG OR MICROSOFT.';

// ---- Search vocabulary ------------------------------------------------------
//
// Use, material and style words are matched directly against the annotation
// fields.  This table only holds what those fields cannot answer on their own:
// intents scored from machine facts (colour from preview Lab, shape class from
// registry tags, light from emission level) and bridges from Chinese or
// building jargon to the words annotations actually use.
//
//   color: COLOR_SCORES id          shapes: acceptable SHAPE_TAGS classes
//   light: wants a light source      alts: further words that satisfy the term
//
// A term with neither alts nor shapes is an intent only and is not matched as
// text: "light" would hit "Light Gray", and "dark" is a lightness preference.
const VOCAB = [
  [['white', '白', '白色', '纯白'], { color: 'white' }],
  [['black', '黑', '黑色'], { color: 'black' }],
  [['gray', 'grey', '灰', '灰色'], { color: 'gray' }],
  [['brown', '棕', '棕色', '褐', '褐色'], { color: 'brown' }],
  [['red', '红', '红色'], { color: 'red' }],
  [['orange', '橙', '橙色', '橘色'], { color: 'orange' }],
  [['yellow', '黄', '黄色'], { color: 'yellow' }],
  [['green', '绿', '绿色'], { color: 'green' }],
  [['cyan', 'teal', '青', '青色'], { color: 'cyan' }],
  [['blue', '蓝', '蓝色'], { color: 'blue' }],
  [['purple', 'violet', '紫', '紫色'], { color: 'purple' }],
  [['pink', '粉', '粉色', '粉红', '粉红色'], { color: 'pink' }],
  [['dark', '深', '深色', '暗', '暗色'], { color: 'dark' }],
  [['pale', '浅', '浅色', '淡色'], { color: 'pale' }],
  [['warm', '暖', '暖色'], { color: 'warm' }],
  [['红砖'], { color: 'red', alts: ['brick', '砖'] }],
  [['light', 'lighting', 'glowing', 'luminous', '光', '发光', '照明', '光源'], { light: true }],
  [['暖光'], { color: 'warm', light: true }],
  [['lamp', 'lamps', '灯', '灯具'], { light: true, alts: ['lamp', 'lantern', 'light source', '灯'] }],
  [['暖光灯'], { color: 'warm', light: true, alts: ['lamp', 'lantern', 'light source', '灯'] }],
  [['lantern', 'lanterns', '灯笼'], { light: true, alts: ['lantern', 'lamp', '灯'] }],
  [['stairs', 'stair', 'staircase', '楼梯', '阶梯'], { shapes: ['stairs'] }],
  [['slab', 'slabs', '台阶', '半砖'], { shapes: ['slab'] }],
  [['fence', 'fences', '栅栏', '篱笆'], { shapes: ['fence'] }],
  [['fence gate', '栅栏门'], { shapes: ['fence_gate'] }],
  [['trapdoor', 'trapdoors', '活板门', '活门'], { shapes: ['trapdoor'] }],
  [['door', 'doors', '门'], { shapes: ['door'] }],
  [['pane', 'panes', 'glass pane', '玻璃板'], { shapes: ['pane'] }],
  [['carpet', 'carpets', '地毯'], { shapes: ['carpet'] }],
  [['button', 'buttons', '按钮'], { shapes: ['button'] }],
  [['pressure plate', '压力板'], { shapes: ['pressure_plate'] }],
  [['围墙'], { shapes: ['wall'], alts: ['wall'] }],
  [['shutter', 'shutters', '百叶', '百叶窗'], { shapes: ['trapdoor'], alts: ['shutter', 'trapdoor'] }],
  [['railing', 'railings', 'balustrade', 'baluster', 'handrail', '栏杆', '护栏', '扶手'], { shapes: ['fence', 'pane', 'wall', 'fence_gate'], alts: ['railing', 'fence', 'bars', 'balustrade'] }],
  [['sill', '窗台'], { shapes: ['slab', 'stairs', 'trapdoor'], alts: ['sill', 'ledge', 'trim'] }],
  [['trim', 'molding', 'moulding', 'cornice', '线脚', '腰线', '饰线', '装饰线'], { alts: ['trim', 'molding', 'border', 'ledge'] }],
  [['beam', 'beams', '梁', '横梁'], { alts: ['beam', 'log', 'pillar'] }],
  [['木梁'], { alts: ['beam', 'log', '原木'] }],
  [['shingle', 'shingles', '瓦', '瓦片', '屋瓦'], { alts: ['shingle', 'roof', 'roofing', 'tile'] }],
  [['plaster', 'stucco', '抹灰', '灰泥', '灰浆', '粉刷'], { alts: ['plaster', 'stucco', 'concrete'] }],
  [['old', 'aged', 'ancient', '旧', '老', '古旧', '破旧', '陈旧'], { alts: ['old', 'weathered', 'cracked', 'mossy', 'aged'] }],
  [['wooden', 'wood', '木', '木头', '木质', '木制', '木材'], { alts: ['wood', 'wooden', 'planks', '木'] }],
  [['墙', '墙面', '墙体', '墙壁'], { alts: ['wall', '墙'] }],
  [['屋顶', '房顶'], { alts: ['roof', 'roofing', '屋顶'] }],
  [['屋檐'], { alts: ['eave', 'roof', '屋檐'] }],
  [['地板', '地面', '铺地'], { alts: ['floor', 'flooring', '地板'] }],
  [['柱', '柱子'], { alts: ['pillar', 'column', '柱'] }],
  [['窗', '窗户'], { alts: ['window', '窗'] }],
  [['窗框'], { alts: ['window frame', 'window', '窗框'] }],
  [['石', '石头', '石材'], { alts: ['stone', '石'] }],
  [['光滑', '平滑'], { alts: ['smooth', '平滑'] }],
  [['砖', '砖块'], { alts: ['brick', '砖'] }],
  [['玻璃'], { alts: ['glass', '玻璃'] }],
  [['金属'], { alts: ['metal', 'metallic', '金属'] }],
  [['铜'], { alts: ['copper', '铜'] }],
  [['铁'], { alts: ['iron', '铁'] }],
  [['苔藓', '青苔', '苔'], { alts: ['moss', 'mossy', '苔'] }],
  [['现代'], { alts: ['modern', '现代'] }],
  [['古典'], { alts: ['classic', 'classical', '古典'] }],
  [['简约', '简单'], { alts: ['minimal', 'simple', 'plain', '简约'] }],
  [['乡村'], { alts: ['rustic', '乡村'] }],
  [['装饰'], { alts: ['decorative', 'decoration', '装饰'] }],
];
const VOCAB_INDEX = new Map(VOCAB.flatMap(([terms, spec]) => terms.map((term) => [term, spec])));
// Words that state a colour in a name or annotation; official zh names use
// the 色 forms (白色混凝土, 淡灰色羊毛).
const COLOR_NAME_WORDS = {
  white: ['white', '白色'], black: ['black', '黑色'], gray: ['gray', 'grey', '灰色'], brown: ['brown', '棕色'],
  red: ['red', '红色'], orange: ['orange', '橙色'], yellow: ['yellow', '黄色'], green: ['green', '绿色'],
  cyan: ['cyan', 'teal', '青色'], blue: ['blue', '蓝色'], purple: ['purple', '紫色'], pink: ['pink', '粉红色'],
  dark: ['dark', '深色'], pale: ['pale', '淡'], warm: ['warm', 'amber', 'orange', 'golden', 'gold', '暖'],
};

// Colour intents over the preview's mean CIELAB as L, chroma, hue.  Previews
// carry the renderer's directional shading, so a white block averages L*
// 64-75 and the thresholds sit on that scale rather than on texture colour.
const ramp = (value, from, to) => clamp01((value - from) / (to - from));
const hueNear = (hue, center, width) => clamp01(1 - Math.abs(((hue - center + 540) % 360) - 180) / width);
const COLOR_SCORES = {
  white: ([l, c]) => ramp(l, 45, 62) * ramp(c, 16, 8),
  black: ([l, c]) => ramp(l, 28, 15) * ramp(c, 20, 10),
  gray: ([l, c]) => Math.min(ramp(l, 10, 22), ramp(l, 62, 48)) * ramp(c, 12, 5),
  brown: ([l, c, h]) => hueNear(h, 60, 40) * ramp(c, 6, 14) * ramp(l, 55, 40),
  red: ([, c, h]) => hueNear(h, 30, 30) * ramp(c, 15, 30),
  orange: ([l, c, h]) => hueNear(h, 55, 20) * ramp(c, 20, 35) * ramp(l, 25, 35),
  yellow: ([l, c, h]) => hueNear(h, 85, 25) * ramp(c, 20, 35) * ramp(l, 35, 50),
  green: ([, c, h]) => hueNear(h, 130, 45) * ramp(c, 12, 25),
  cyan: ([, c, h]) => hueNear(h, 200, 35) * ramp(c, 8, 20),
  blue: ([, c, h]) => hueNear(h, 265, 40) * ramp(c, 12, 25),
  purple: ([, c, h]) => hueNear(h, 310, 35) * ramp(c, 12, 25),
  pink: ([l, c, h]) => hueNear(h, 355, 35) * ramp(c, 10, 25) * ramp(l, 35, 50),
  dark: ([l]) => ramp(l, 40, 20),
  pale: ([l]) => ramp(l, 45, 62),
  warm: ([, c, h]) => hueNear(h, 55, 45) * ramp(c, 8, 20),
};
const lch = (lab) => {
  if (!Array.isArray(lab) || lab.length !== 3 || lab.some((value) => typeof value !== 'number')) return null;
  const [l, a, b] = lab;
  return [l, Math.hypot(a, b), ((Math.atan2(b, a) * 180) / Math.PI + 360) % 360];
};

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
const WALL_MOUNTED_RE = /(?:^|_)wall_/;
const WALL_WORDS = new Set(['wall', '墙']);
const WALL_SHAPE_WEIGHT = 0.6;
// When the query names no shape, derived forms of a material rank below the
// base block, and small fixtures that only borrow its texture rank lower still.
const SHAPE_FACTORS = new Map([
  ...['stairs', 'slab', 'wall', 'fence_gate', 'fence', 'trapdoor', 'door', 'carpet', 'pane'].map((shape) => [shape, 0.9]),
  ...['button', 'pressure_plate', 'sign', 'banner', 'wall_mounted', 'rod', 'chain'].map((shape) => [shape, 0.8]),
]);
// The 16 dye colours in creative-inventory order.  A colour series is a block
// ID suffix that all 16 carry (white_wool ... black_wool); tulips or red and
// brown mushrooms are not one.  Longer names first so light_blue_ is not read
// as blue_.
const DYE_COLORS = [
  'white', 'light_gray', 'gray', 'black', 'brown', 'red', 'orange', 'yellow',
  'lime', 'green', 'cyan', 'light_blue', 'blue', 'purple', 'magenta', 'pink',
];
const DYE_PREFIXES = [...DYE_COLORS].sort((left, right) => right.length - left.length);
const colorSeries = (blockId, blockIds) => {
  const path = blockId.replace(/^minecraft:/, '');
  const color = DYE_PREFIXES.find((dye) => path.startsWith(`${dye}_`));
  if (color === undefined) return null;
  const suffix = path.slice(color.length + 1);
  if (!DYE_COLORS.every((dye) => blockIds.has(`minecraft:${dye}_${suffix}`))) return null;
  return { key: suffix, color };
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
  return Object.keys(forms).length === 0 ? null : { base_block: familyBase(stem, blockIds), forms };
};
// { base_block, forms } for a block that is a material's full block or one of
// its forms; null otherwise (bamboo_block is not the bamboo planks).
const materialFamily = (blockId, blockIds) => {
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
const hasBuildingForms = (blockId, blockIds) => {
  const family = materialFamily(blockId, blockIds);
  return family?.base_block === blockId && ['stairs', 'slab', 'wall'].some((form) => form in family.forms);
};
const shapeClass = (blockId, tags = []) => {
  const tagged = SHAPE_TAGS.find(([, tag]) => tags.includes(tag));
  if (tagged !== undefined) return tagged[0];
  if (WALL_MOUNTED_RE.test(blockId.replace(/^minecraft:/, ''))) return 'wall_mounted';
  return SHAPE_ID_SUFFIXES.find(([, suffix]) => blockId.endsWith(suffix))?.[0] ?? null;
};
// Shape class for comparison and similarity: the tag class, else full_cube or
// other from the representative's geometry.
const shapeKey = (blockId, tags, geometry) => shapeClass(blockId, tags) ?? (geometry?.is_full_cube === true ? 'full_cube' : 'other');
// Palette similarity is 1 at identical colour and texture and 0 at this
// distance (Oklab plus the texture term) or beyond.
const SIMILARITY_RANGE = 0.25;

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

// Only fields a host can act on: which versions exist, which IDs were unknown.
const errorResult = (error, invalidBlockIds = []) => {
  const structuredContent = {
    error_code: ERROR_CODES.has(error.code) ? error.code : 'MCP_INTERNAL_ERROR',
    message: String(error.message).slice(0, 500),
  };
  if (error.availableVersions?.length) structuredContent.available_versions = [...error.availableVersions];
  if (invalidBlockIds.length) structuredContent.invalid_block_ids = [...invalidBlockIds];
  return { structuredContent, images: [], isError: true };
};

const toolResult = (output, images = []) => ({ structuredContent: output, images: [...images], isError: false });

// ---- Query terms and field matching ----------------------------------------
const HAN_RE = /\p{Script=Han}/u;
const HAN_RUN_RE = /(\p{Script=Han}+)/u;
// Plural folding only, applied to query and field words alike.
const stem = (word) => {
  if (word.length > 3 && word.endsWith('ies')) return `${word.slice(0, -3)}y`;
  if (word.length > 3 && word.endsWith('s') && !word.endsWith('ss')) return word.slice(0, -1);
  return word;
};
const wordsOf = (text) => (text.match(/[\p{L}\p{N}]+/gu) ?? []).map(stem);

const ALT_WEIGHT = 0.85;
// A Han alt that is part of the query word itself is the same morpheme (苔 of
// 苔藓 in 苔石, mossy cobblestone), not a translation.
const MORPHEME_WEIGHT = 0.95;
const BREADTH_RANGE = 0.1;
const FIELD_WEIGHTS = {
  name: 1.0, synonym: 0.9, role: 0.6, material: 0.6, style: 0.6, 'shape term': 0.6, 'color term': 0.6, 'machine tag': 0.6, summary: 0.35,
};
// avoid_for is deliberately absent: its entries mix unsuitable uses with
// look-alike blocks, so they never count as a positive match.
const ANNOTATION_FIELDS = [
  ['synonym', 'synonyms_zh'], ['synonym', 'synonyms_en'], ['role', 'building_roles'], ['material', 'material_impressions'],
  ['style', 'style_tags'], ['shape term', 'shape_terms'], ['color term', 'color_terms'], ['summary', 'summary_zh'], ['summary', 'summary_en'],
];
const DIM_WEIGHTS = { text: 0.5, color: 0.3, shape: 0.25, light: 0.25 };

// Start indices where needle occurs as a contiguous run in haystack.
const runStarts = (haystack, needle) => {
  const starts = [];
  for (let start = 0; start + needle.length <= haystack.length; start += 1) {
    if (needle.every((part, offset) => haystack[start + offset] === part)) starts.push(start);
  }
  return starts;
};

// One matcher per query word: Han by substring (no word boundaries), others by
// whole stemmed words in order.
const matcher = (text) => {
  if (HAN_RE.test(text)) return { text, han: true, test: (item) => item.raw.includes(text) };
  const words = wordsOf(text);
  return { text, han: false, words, test: (item) => runStarts(item.words, words).length > 0 };
};

const fieldItem = (field, text) => {
  const raw = normalized(text);
  return { field, weight: FIELD_WEIGHTS[field], raw, words: wordsOf(raw) };
};

// Longest-match segmentation of an unspaced Han run; characters no lexicon
// entry starts with stay together as one term.
const segmentHan = (run, lexicon) => {
  const chars = [...run];
  const out = [];
  let pending = '';
  for (let index = 0; index < chars.length;) {
    let hit = '';
    for (let length = Math.min(8, chars.length - index); length >= 1 && !hit; length -= 1) {
      const candidate = chars.slice(index, index + length).join('');
      if (lexicon.has(candidate)) hit = candidate;
    }
    if (hit) {
      if (pending) out.push(pending);
      pending = '';
      out.push(hit);
      index += [...hit].length;
    } else {
      pending += chars[index];
      index += 1;
    }
  }
  if (pending) out.push(pending);
  return out;
};

const queryTerm = (text) => {
  const spec = VOCAB_INDEX.get(text) ?? {};
  const textual = spec.alts !== undefined || spec.shapes !== undefined || (spec.color === undefined && !spec.light);
  const alts = textual ? [...new Set([text, ...(spec.alts ?? [])])] : [];
  return {
    text,
    textual,
    matchers: alts.map((alt, index) => ({
      ...matcher(alt),
      weight: index === 0 ? 1 : HAN_RE.test(alt) && text.includes(alt) ? MORPHEME_WEIGHT : ALT_WEIGHT,
    })),
    color: spec.color ?? null,
    shapes: spec.shapes ?? [],
    light: spec.light === true,
  };
};

// Keywords become terms: two-word vocabulary entries ("fence gate") merge,
// Han runs are segmented.  Returns the terms plus the union of their intents.
const parseQuery = (keywords, lexicon) => {
  const texts = [];
  for (const keyword of keywords) {
    const words = normalized(keyword).split(' ').filter(Boolean)
      .flatMap((piece) => piece.split(HAN_RUN_RE).filter(Boolean).flatMap((part) => (HAN_RE.test(part) ? segmentHan(part, lexicon) : [part])));
    for (let index = 0; index < words.length; index += 1) {
      const pair = `${words[index]} ${words[index + 1]}`;
      if (index + 1 < words.length && VOCAB_INDEX.has(pair)) {
        texts.push(pair);
        index += 1;
      } else {
        texts.push(words[index]);
      }
    }
  }
  const unique = [...new Set(texts)];
  // One field word satisfies at most one query term: an alt that is another
  // query word ("roof" for "shingle" in "roof shingle") or an earlier term's
  // alt ("mossy" for both 苔藓 and 旧) is dropped.
  const claimed = new Set(unique);
  const terms = unique.map(queryTerm).map((term) => {
    const matchers = term.matchers.filter((candidate, index) => index === 0 || !claimed.has(candidate.text));
    for (const candidate of matchers) claimed.add(candidate.text);
    return { ...term, matchers };
  });
  const phrase = normalized(keywords.join(' '));
  return {
    terms,
    textTerms: terms.filter((term) => term.textual),
    colors: [...new Set(terms.map((term) => term.color).filter(Boolean))],
    shapes: new Set(terms.flatMap((term) => term.shapes)),
    light: terms.some((term) => term.light),
    phrase: terms.length > 1 ? matcher(HAN_RE.test(phrase) ? phrase.replace(/ /g, '') : phrase) : null,
  };
};

// Fraction of a name's words (Han: characters) that some matching query word
// covers, so "stone" prefers "Stone" to "Stone Button".
const nameCoverage = (item, matchers) => {
  const han = HAN_RE.test(item.raw);
  const units = han ? [...item.raw] : item.words;
  const covered = new Array(units.length).fill(false);
  for (const candidate of matchers) {
    if (candidate.han !== han) continue;
    const needle = han ? [...candidate.text] : candidate.words;
    for (const start of runStarts(units, needle)) covered.fill(true, start, start + needle.length);
  }
  return covered.filter(Boolean).length / Math.max(1, units.length);
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

const RELATIVE_COLORS = new Set(['dark', 'pale']);
const NAMED_COLOR_WEIGHTS = { name: 1.0, 'color term': 0.8, style: 0.8, summary: 0.5 };
const COLOR_MATCHERS = Object.fromEntries(Object.entries(COLOR_NAME_WORDS).map(([color, words]) => [color, words.map(matcher)]));

// Relevance of one candidate document to a parsed query.  Dimensions are
// averaged over the ones the query asks for, so a pure colour query is not
// diluted by an empty text score.
const scoreDocument = (doc, query) => {
  const breakdown = { text: 0, color: 0, shape: 0, light: 0 };
  const present = [];
  const notes = [];
  if (query.textTerms.length > 0) {
    present.push('text');
    let total = 0;
    const hits = [];
    const nameMatchers = [];
    for (const term of query.textTerms) {
      let best = 0;
      let where = null;
      const fields = new Set();
      for (const item of doc.fields) {
        for (const candidate of term.matchers) {
          const wallWord = WALL_WORDS.has(candidate.text);
          if (doc.mounted && wallWord) continue;
          const fieldWeight = wallWord && doc.shape === 'wall' ? Math.min(item.weight, WALL_SHAPE_WEIGHT) : item.weight;
          const value = fieldWeight * candidate.weight;
          if ((value > best || !fields.has(item.field)) && candidate.test(item)) {
            fields.add(item.field);
            if (value > best) {
              best = value;
              where = item.field;
            }
          }
        }
      }
      // Several annotation fields agreeing (name, role "aged walls", style
      // "weathered") is stronger evidence than one stray synonym.
      total += best * (1 - BREADTH_RANGE + BREADTH_RANGE * Math.min(1, (fields.size - 1) / 2));
      if (where !== null) hits.push(`${term.text} (${where})`);
      nameMatchers.push(...term.matchers.filter((candidate) => !(WALL_WORDS.has(candidate.text) && (doc.mounted || doc.shape === 'wall'))
        && doc.names.some((item) => candidate.test(item))));
    }
    for (const color of query.colors) nameMatchers.push(...COLOR_MATCHERS[color]);
    const specificity = Math.max(0, ...doc.names.map((item) => nameCoverage(item, nameMatchers)));
    let text = (total / query.textTerms.length) * (0.85 + 0.15 * specificity);
    if (query.phrase !== null) {
      const phrase = Math.max(0, ...doc.fields.filter((item) => query.phrase.test(item)).map((item) => item.weight));
      text = 0.9 * text + 0.1 * phrase;
      if (phrase > 0) hits.push('whole phrase');
    }
    breakdown.text = text;
    if (hits.length > 0) notes.push(`matches ${hits.join(', ')}`);
  }
  if (query.colors.length > 0) {
    present.push('color');
    let total = 0;
    for (const color of query.colors) {
      // A preview shows the fixture's body, not the colour of the light it
      // gives, so "warm light" rests on what the annotation says.
      const measured = doc.lch === null || (color === 'warm' && query.light) ? 0 : COLOR_SCORES[color](doc.lch);
      let named = 0;
      for (const item of doc.colorItems) {
        if (COLOR_MATCHERS[color].some((candidate) => candidate.test(item))) named = Math.max(named, NAMED_COLOR_WEIGHTS[item.field]);
      }
      // Stating the colour, above all in the name, edges out a block that only
      // measures as it.  "Dark"/"pale" in a name are relative to the base
      // material (Dark Prismarine is lighter than blackstone), so lightness
      // rests on the measurement.
      const base = RELATIVE_COLORS.has(color) ? measured : Math.max(measured, named);
      total += 0.85 * base + 0.15 * named;
    }
    breakdown.color = total / query.colors.length;
    if (breakdown.color > 0) notes.push(`${query.colors.join('+')} colour ${breakdown.color.toFixed(2)}`);
  }
  if (query.shapes.size > 0) {
    present.push('shape');
    breakdown.shape = doc.shape !== null && query.shapes.has(doc.shape) ? 1 : 0;
    if (breakdown.shape > 0) notes.push(`shape ${doc.shape}`);
  }
  if (query.light) {
    present.push('light');
    breakdown.light = doc.light;
    if (doc.light > 0) notes.push(`light level up to ${Math.round(doc.light * 15)}`);
  }
  let weight = 0;
  let score = 0;
  for (const key of present) {
    breakdown[key] = pyRound8(clamp01(breakdown[key]));
    weight += DIM_WEIGHTS[key];
    score += DIM_WEIGHTS[key] * breakdown[key];
  }
  score = weight === 0 ? 0 : score / weight;
  const factor = query.shapes.size === 0 ? SHAPE_FACTORS.get(doc.shape) : undefined;
  if (factor !== undefined) {
    score *= factor;
    if (score > 0) notes.push(`${doc.shape} form ×${factor} (no shape asked)`);
  }
  return { score: pyRound8(clamp01(score)), breakdown, notes };
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

const variantsFor = (snapshot, blockId) =>
  Object.values(snapshot.variants)
    .filter((variant) => variant.block_id === blockId)
    .sort((left, right) => byUtf8(String(left.variant_id), String(right.variant_id)));

const DETAIL_MODES = new Set(['summary', 'states']);
const pageInteger = (args, key, fallback, min, max) => {
  const value = key in args ? args[key] : fallback;
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < min || value > max) {
    throw new MCPInputError(`${key} must be an integer from ${min} to ${max}`);
  }
  return value;
};

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

  searchBlocks(argumentsValue) {
    const resources = { previewCache: new Map() };
    try {
      const args = validateObject(argumentsValue, new Set(['minecraft_version', 'keywords', 'limit', 'similar_to']));
      const version = validateVersionInput(args);
      const similarTo = 'similar_to' in args ? args.similar_to : null;
      if ('similar_to' in args && !fullMatch(BLOCK_ID_RE, similarTo)) throw new MCPInputError('similar_to has an invalid format');
      if (!('keywords' in args) && similarTo === null) throw new MCPInputError('keywords or similar_to is required');
      const [keywords, joinedQuery] = 'keywords' in args ? MCPQueryService._validateKeywords(args.keywords) : [null, null];
      // Python's args.get("limit", 8): a present-but-null key stays null and
      // fails validation, so the default only applies when the key is absent.
      const limit = 'limit' in args ? args.limit : 8;
      if (typeof limit !== 'number' || !Number.isInteger(limit) || limit < 1 || limit > 12) {
        throw new MCPInputError('limit must be an integer from 1 to 12');
      }
      const handle = this.resolver.resolve(version);
      try {
        const snapshot = this._snapshot(handle);
        let ranked = null;
        let query = null;
        if (keywords !== null) {
          query = parseQuery(keywords, this._searchIndex(snapshot).lexicon);
          const exactQuery = normalized(joinedQuery);
          const recalled = this._recall(handle, this._eligibleRows(snapshot), query, exactQuery);
          ranked = this._rankRows(recalled, snapshot, query, exactQuery);
        }
        let selected;
        if (similarTo === null) {
          const merged = query.colors.length > 0 ? ranked : MCPQueryService._mergeColorSeries(ranked);
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
          const similar = this._rankSimilar(handle, snapshot, similarTo, ranked, resources);
          if (similar === null) {
            return errorResult(
              new MCPReleaseError('BLOCK_HAS_NO_PREVIEW', 'The similar_to block has no preview to compare colours with.', {
                minecraftVersion: handle.minecraftVersion,
              }),
              [similarTo],
            );
          }
          selected = similar.slice(0, limit);
        }
        const candidates = this._candidateDicts(selected, snapshot);
        if (candidates.length === 0) return toolResult({ candidates, images: [] });
        const sheet = this._contactSheet(
          handle,
          candidates.map((candidate, index) => ({ candidateId: candidate.candidate_id, blockId: candidate.block_id, variantId: selected[index].row[0] })),
          4,
          resources,
        );
        return toolResult({ candidates, images: [sheet.image] }, [sheet.webp]);
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
        if (page !== null) return toolResult(this._statesPage(handle, snapshot, blockId, page));
        const [output, images] = this._detailsSummary(handle, snapshot, blockId, resources);
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
      const args = validateObject(argumentsValue, new Set(['minecraft_version', 'block_ids']));
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
        const [output, images] = this._compareData(handle, snapshot, blockIds, resources);
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

  // Per-release search documents and the Han segmentation lexicon, built once
  // per snapshot: vocabulary words, official zh names and name prefixes that
  // at least three names share (橡木, 深色橡木, 石砖).
  _searchIndex(snapshot) {
    if (snapshot.search !== undefined) return snapshot.search;
    // A light source counts by its brightest legal state: a redstone lamp's
    // canonical state is unlit.
    const emission = new Map();
    for (const state of Object.values(snapshot.states)) {
      const level = Number(state.behavior?.emission_level);
      if (Number.isFinite(level) && level > (emission.get(state.block_id) ?? 0)) emission.set(state.block_id, level);
    }
    const docs = new Map();
    const blockIds = new Set(Object.keys(snapshot.blocks));
    for (const [variantId, variant] of Object.entries(snapshot.variants)) {
      const block = snapshot.blocks[String(variant.block_id)];
      if (block === undefined) continue;
      const names = block.official_names ?? {};
      const semanticValue = semantic(snapshot.annotations[variantId]);
      const nameItems = [names.zh_cn, names.en_us, String(variant.block_id).replace(/^minecraft:/, '').replace(/_/g, ' ')]
        .filter((value) => typeof value === 'string' && value)
        .map((value) => fieldItem('name', value));
      // Everything the recall text holds is scoreable, except avoid_for.
      const fields = [...nameItems, ...(variant.machine_facts?.machine_tags ?? []).map((tag) => fieldItem('machine tag', String(tag)))];
      for (const [field, key] of ANNOTATION_FIELDS) {
        const value = semanticValue[key];
        for (const text of Array.isArray(value) ? value : [value]) {
          if (typeof text === 'string' && text) fields.push(fieldItem(field, text));
        }
      }
      const level = emission.get(String(variant.block_id)) ?? 0;
      docs.set(variantId, {
        fields,
        names: nameItems,
        colorItems: fields.filter((item) => ['name', 'color term', 'style', 'summary'].includes(item.field)),
        // A wall sign keeps its sign class but is still wall-mounted.
        mounted: WALL_MOUNTED_RE.test(String(variant.block_id).replace(/^minecraft:/, '')),
        shape: shapeClass(String(variant.block_id), block.tags ?? []),
        light: clamp01(level / 15),
        lch: lch(snapshot.features[variantId]?.lab),
        confidence: typeof semanticValue.confidence === 'number' ? semanticValue.confidence : 0,
        buildingForms: hasBuildingForms(String(variant.block_id), blockIds),
        series: colorSeries(String(variant.block_id), blockIds),
      });
    }
    const lexicon = new Set([...VOCAB_INDEX.keys()].filter((term) => HAN_RE.test(term)));
    const prefixes = new Map();
    for (const block of Object.values(snapshot.blocks)) {
      const name = block.official_names?.zh_cn;
      if (typeof name !== 'string' || !HAN_RE.test(name)) continue;
      lexicon.add(normalized(name));
      const chars = [...normalized(name)];
      for (let length = 2; length <= Math.min(4, chars.length - 1); length += 1) {
        const prefix = chars.slice(0, length).join('');
        prefixes.set(prefix, (prefixes.get(prefix) ?? 0) + 1);
      }
    }
    for (const [prefix, count] of prefixes) if (count >= 3) lexicon.add(prefix);
    snapshot.search = { docs, lexicon };
    return snapshot.search;
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

  // Exact official names and IDs first; then score.  Ties go to annotation
  // confidence, then (when no shape was asked) base blocks over their derived
  // forms, then construction materials, then eligible over conditional, then
  // variant ID.
  _rankRows(rows, snapshot, query, exactQuery) {
    const { docs } = this._searchIndex(snapshot);
    const result = [];
    for (const row of rows) {
      const [variantId, variant, , block] = row;
      const doc = docs.get(variantId);
      const exact = exactBlockQuery(exactQuery, variant.block_id, block.official_names ?? {});
      const { score, breakdown, notes } = scoreDocument(doc, query);
      if (!exact && score <= 0) continue;
      const penalized = policyWarning(variant.block_id) !== null && !exact;
      result.push({
        row,
        // A full official name or block ID is a perfect match by definition.
        score: exact ? 1 : penalized ? pyRound8(score * 0.25) : score,
        breakdown,
        exact,
        penalized,
        notes,
        confidence: doc.confidence,
        derived: query.shapes.size === 0 && SHAPE_FACTORS.has(doc.shape),
        buildingForms: doc.buildingForms,
        series: doc.series,
        conditional: variant.candidate_qualification === 'conditional',
      });
    }
    result.sort((left, right) => Number(right.exact) - Number(left.exact)
      || right.score - left.score
      || right.confidence - left.confidence
      || Number(left.derived) - Number(right.derived)
      || Number(right.buildingForms) - Number(left.buildingForms)
      || Number(left.conditional) - Number(right.conditional)
      || byUtf8(left.row[0], right.row[0]));
    return result;
  }

  // Blocks of the same shape class as `targetId`, nearest palette first.  With
  // keywords, `textRanked` (their ranked matches) is the pool and the score is
  // still the palette similarity alone.  Null when the target has no preview
  // or no visible face.  Recommendation-list blocks keep the ×0.25 penalty,
  // so infested stone does not lead a search for stone.
  _rankSimilar(handle, snapshot, targetId, textRanked, resources) {
    const target = variantsFor(snapshot, targetId)[0];
    if (target === undefined || !isMapping(target.render)) return null;
    const reference = this._palette(handle, snapshot, String(target.variant_id), resources);
    if (reference.top === null && reference.side === null) return null;
    const targetShape = shapeKey(targetId, snapshot.blocks[targetId].tags ?? [], target.machine_facts?.geometry);
    const pool = textRanked ?? this._eligibleRows(snapshot).map((row) => ({ row, breakdown: { text: 0, color: 0, shape: 0, light: 0 } }));
    const result = [];
    for (const { row, breakdown } of pool) {
      const [variantId, variant, , block] = row;
      if (variant.block_id === targetId) continue;
      if (shapeKey(String(variant.block_id), block.tags ?? [], variant.machine_facts?.geometry) !== targetShape) continue;
      const distance = paletteDistance(reference, this._palette(handle, snapshot, variantId, resources));
      if (distance === null) continue;
      const similarity = pyRound8(clamp01(1 - distance.distance / SIMILARITY_RANGE));
      if (similarity <= 0) continue;
      const penalized = policyWarning(variant.block_id) !== null;
      const colorDeltaE = Number((distance.color * 100).toFixed(1));
      result.push({
        row,
        score: penalized ? pyRound8(similarity * 0.25) : similarity,
        breakdown: { text: breakdown.text, color: similarity, shape: 1, light: 0 },
        penalized,
        notes: [
          `palette similarity ${similarity.toFixed(2)} to ${targetId}: colour ΔE ${colorDeltaE} (Oklab ×100 over top and side faces), texture term ${(distance.texture * 100).toFixed(1)}`,
          `same shape class ${targetShape}`,
          ...(textRanked === null ? [] : ['keywords matched']),
        ],
        colorDeltaE,
        distance: distance.distance,
      });
    }
    result.sort((left, right) => right.score - left.score
      || left.distance - right.distance
      || byUtf8(left.row[0], right.row[0]));
    return result;
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

  // Without a colour in the query, the 16 dye colours of one series collapse
  // into their best-ranked member (white first among equal scores), placed
  // where the series first appears.  An exact name or ID keeps its own entry.
  static _mergeColorSeries(ranked) {
    const bySeries = new Map();
    for (const entry of ranked) {
      if (entry.series === null || entry.exact) continue;
      const members = bySeries.get(entry.series.key) ?? [];
      members.push(entry);
      bySeries.set(entry.series.key, members);
    }
    const out = [];
    for (const entry of ranked) {
      const members = entry.series === null || entry.exact ? undefined : bySeries.get(entry.series.key);
      if (members === undefined) {
        out.push(entry);
        continue;
      }
      if (members[0] !== entry) continue;
      const best = members.filter((member) => member.score === entry.score)
        .sort((left, right) => DYE_COLORS.indexOf(left.series.color) - DYE_COLORS.indexOf(right.series.color))[0];
      if (members.length === 1) {
        out.push(best);
        continue;
      }
      const others = DYE_COLORS.filter((dye) => dye !== best.series.color && members.some((member) => member.series.color === dye));
      out.push({ ...best, others });
    }
    return out;
  }

  _candidateDicts(ranked, snapshot) {
    return ranked.map(({ row, score, breakdown, penalized, notes, series, others, colorDeltaE }, index) => {
      const [variantId, variant, , block] = row;
      const names = block.official_names ?? {};
      const reason = MCPQueryService._reason(notes, semantic(snapshot.annotations[variantId]));
      return {
        candidate_id: `T${String(index + 1).padStart(2, '0')}`,
        block_id: String(variant.block_id),
        // Python str(names.get("zh_cn") or names.get("en_us") or variant_id):
        // an empty string falls through, so the schema minLength 1 still holds.
        display_name: String(names.zh_cn || names.en_us || variantId),
        recommended_state_id: String(variant.canonical_state_id),
        candidate_qualification: String(variant.candidate_qualification),
        score,
        score_breakdown: breakdown,
        reason: penalized ? `${reason.slice(0, 450)} Local recommendation rule: general-use score ×0.25.` : reason,
        warnings: blockWarnings(variant, String(variant.block_id), block),
        ...(colorDeltaE === undefined ? {} : { color_delta_e: colorDeltaE }),
        ...(others === undefined ? {} : {
          color_series: { block_id_pattern: `minecraft:{color}_${series.key}`, other_colors: others },
        }),
      };
    });
  }

  static _reason(notes, semanticValue) {
    if (notes.length > 0) {
      const text = `${notes.join('; ')}.`;
      return text.charAt(0).toUpperCase() + text.slice(1, 500);
    }
    const summary = semanticValue.summary_en;
    return typeof summary === 'string' && summary ? summary.slice(0, 500) : 'Deterministic release candidate.';
  }

  // tiles: [{ candidateId, blockId, variantId }] in sheet order; the painted
  // T01.. label of each card is its candidateId.
  _contactSheet(handle, tiles, columns, resources) {
    const previews = tiles.map((tile) => this._preview(handle, tile.variantId, resources).decoded);
    const sheet = makeContactSheet(previews, columns);
    const cols = Math.max(1, Math.min(columns, tiles.length));
    const image = {
      content_index: 1,
      mime_type: IMAGE_MIME_TYPE,
      width: sheet.width,
      height: sheet.height,
      tiles: tiles.map((tile, index) => ({
        candidate_id: tile.candidateId,
        block_id: tile.blockId,
        row: Math.floor(index / cols),
        column: index % cols,
      })),
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

  // A block's summary describes one representative: the canonical state of its
  // first visual variant (the default state when it has none).  Geometry and the
  // image belong to that state only; every legal state is read through
  // detail="states".
  _detailsSummary(handle, snapshot, blockId, resources) {
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
        shape: boxes(state.shape),
        collision: boxes(state.collision),
        geometry_classes: [...(geometry.geometry_classes ?? [])],
        machine_tags: [...(variant?.machine_facts?.machine_tags ?? [])],
        behavior: behaviorOutput(variant === undefined ? state.behavior : behavior(variant, state)),
      },
      semantics: annotation === undefined ? null : semantic(annotation),
      warnings,
      images: [],
    };
    const family = materialFamily(blockId, new Set(Object.keys(snapshot.blocks)));
    if (family !== null) output.family = family;
    const skip = MCPQueryService._skipReason(snapshot.manual, blockId);
    if (variant === undefined && skip !== null) output.skip_reason = skip;
    if (variant === undefined || !isMapping(variant.render)) return [output, []];
    const card = makeBlockCard(this._preview(handle, String(variant.variant_id), resources).decoded);
    const colors = paletteOutput(this._palette(handle, snapshot, String(variant.variant_id), resources));
    if (colors !== null) output.representative.colors = colors;
    output.images.push({ content_index: 1, mime_type: IMAGE_MIME_TYPE, width: card.width, height: card.height, state_id: stateId });
    return [output, [card.webp]];
  }

  // Real release states in UTF-8 byte order of state_id; never synthesized from
  // the property cross product.  release_id lets a client restart paging when
  // the published release changes between pages.
  _statesPage(handle, snapshot, blockId, { offset, limit }) {
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
        shape: boxes(state.shape),
        collision: boxes(state.collision),
        behavior: behaviorOutput(state.behavior),
        variant_ids: [...state.variant_ids],
        mapping_status: state.mapping_status,
      })),
    };
  }

  static _skipReason(manual, blockId) {
    const reviews = Array.isArray(manual.skip_reviews) ? manual.skip_reviews : [];
    const review = reviews.find((item) => isMapping(item) && (item.target_id === blockId || item.block_id === blockId));
    return typeof review?.reason_code === 'string' && review.reason_code ? review.reason_code : null;
  }

  // Every block's comparable facts side by side, in block_ids order; each
  // block describes its representative (first visual variant) like details.
  // differing_fields names the fields whose values are not all equal.
  _compareData(handle, snapshot, blockIds, resources) {
    const allIds = new Set(Object.keys(snapshot.blocks));
    const tiles = [];
    const blocks = blockIds.map((blockId) => {
      const block = snapshot.blocks[blockId];
      const names = block.official_names ?? {};
      const variant = variantsFor(snapshot, blockId)[0];
      const entry = { block_id: blockId, display_name: String(names.zh_cn || names.en_us || blockId) };
      if (variant !== undefined) {
        entry.candidate_id = `T${String(tiles.length + 1).padStart(2, '0')}`;
        tiles.push({ candidateId: entry.candidate_id, blockId, variantId: String(variant.variant_id) });
      }
      const state = snapshot.states[String(variant === undefined ? block.default_state_id : variant.canonical_state_id)] ?? {};
      const facts = variant === undefined ? (isMapping(state.behavior) ? state.behavior : {}) : behavior(variant, state);
      const annotation = variant === undefined ? undefined : snapshot.annotations[String(variant.variant_id)];
      const semanticValue = semantic(annotation);
      const colors = variant === undefined || !isMapping(variant.render)
        ? null
        : paletteOutput(this._palette(handle, snapshot, String(variant.variant_id), resources));
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
        family: materialFamily(blockId, allIds),
        warnings: blockWarnings(variant, blockId, block),
      };
    });
    const differing = COMPARE_FIELDS.filter((field) => new Set(blocks.map((item) => JSON.stringify(item[field]))).size > 1);
    if (tiles.length === 0) return [{ blocks, differing_fields: differing, images: [] }, []];
    const sheet = this._contactSheet(handle, tiles, tiles.length, resources);
    return [{ blocks, differing_fields: differing, images: [sheet.image] }, [sheet.webp]];
  }
}

const COMPARE_SEMANTIC_KEYS = [
  'summary_zh', 'summary_en', 'color_terms', 'material_impressions', 'style_tags', 'building_roles',
];
const COMPARE_FIELDS = [
  'candidate_qualification', 'shape_class', 'geometry_classes', 'transparent', 'emission_level',
  'redstone_related', 'colors', 'semantics', 'family', 'warnings',
];

export const QueryService = MCPQueryService;
