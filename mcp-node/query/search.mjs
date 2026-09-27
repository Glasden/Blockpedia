import { normalized, clamp01, pyRound8, byUtf8 } from './text.mjs';
import {
  WALL_MOUNTED_RE, WALL_WORDS, WALL_SHAPE_WEIGHT, SHAPE_FACTORS, NON_BUILDING_FACTOR,
  shapeClass, hasBuildingForms, colorSeries, oxidationSeries, lookSeries, blockCategory, policyWarning,
} from './blocks.mjs';
import { semantic } from './output.mjs';

export const exactBlockQuery = (query, blockId, names) =>
  query === normalized(blockId)
  || [names.zh_cn, names.en_us].some((name) => name && query === normalized(name));

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
  // "Red brick" names the ordinary fired-clay brick, whose colour is a
  // brownish brick red rather than the saturated red of red nether bricks.
  [['红砖', 'red brick', 'red bricks'], { color: 'brick', alts: ['brick', '砖'] }],
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
  // Roof shingles are laid as stairs and slabs.  "Tile" is not an alt: the
  // annotations use it for glazed terracotta and carpets.
  [['shingle', 'shingles', '瓦', '瓦片', '屋瓦'], { shapes: ['stairs', 'slab'], alts: ['shingle', 'roof', 'roofing'] }],
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
// the 色 forms (白色混凝土, 淡灰色羊毛).  "Gold" is not a warm word: in a name
// it is the metal (Deepslate Gold Ore).
const COLOR_NAME_WORDS = {
  white: ['white', '白色'], black: ['black', '黑色'], gray: ['gray', 'grey', '灰色'], brown: ['brown', '棕色'],
  red: ['red', '红色'], orange: ['orange', '橙色'], yellow: ['yellow', '黄色'], green: ['green', '绿色'],
  cyan: ['cyan', 'teal', '青色'], blue: ['blue', '蓝色'], purple: ['purple', '紫色'], pink: ['pink', '粉红色'],
  dark: ['dark', '深色'], pale: ['pale', '淡'], warm: ['warm', 'amber', 'orange', 'golden', '暖'],
  brick: ['brick red', 'red brown', '砖红', '红棕'],
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
  // Fired-clay brick: bricks measure about L* 35, chroma 21, hue 41.
  brick: ([l, c, h]) => hueNear(h, 40, 18) * ramp(c, 10, 18) * Math.min(ramp(l, 18, 26), ramp(l, 55, 45)),
};
const lch = (lab) => {
  if (!Array.isArray(lab) || lab.length !== 3 || lab.some((value) => typeof value !== 'number')) return null;
  const [l, a, b] = lab;
  return [l, Math.hypot(a, b), ((Math.atan2(b, a) * 180) / Math.PI + 360) % 360];
};

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
// whole stemmed words in order.  head() is whether it matches the end of the
// text, the head noun of an English or Chinese name (Copper Ore, 铜矿石).
const matcher = (text) => {
  if (HAN_RE.test(text)) return { text, han: true, test: (item) => item.raw.includes(text), head: (item) => item.raw.endsWith(text) };
  const words = wordsOf(text);
  return {
    text,
    han: false,
    words,
    test: (item) => runStarts(item.words, words).length > 0,
    head: (item) => runStarts(item.words, words).some((start) => start + words.length === item.words.length),
  };
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
export const parseQuery = (keywords, lexicon) => {
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
  // Whether some query word is the head of the block's name or a synonym
  // ("ore" for Copper Ore, not "copper"), which asks for it even when it is
  // not a building material.
  let asked = false;
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
          if ((item.field === 'name' || item.field === 'synonym') && !asked && candidate.head(item)) asked = true;
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
  return { score: pyRound8(clamp01(score)), breakdown, notes, asked };
};

// A keyword match scoring under this fraction of the best non-exact match is
// not returned: after the glass blocks, "glass" would otherwise go on to
// beacons, lanterns and obsidian ("volcanic glass") by material words alone.
const RELEVANCE_FLOOR = 0.55;

// Per-release search documents and the Han segmentation lexicon, built once
// per snapshot: vocabulary words, official zh names and name prefixes that
// at least three names share (橡木, 深色橡木, 石砖).
export function searchIndex(snapshot) {
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
      oxidation: oxidationSeries(String(variant.block_id), blockIds),
      lookSeries: lookSeries(String(variant.block_id), blockIds),
      category: blockCategory(String(variant.block_id), block.tags ?? []),
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

// Exact official names and IDs first; then score.  Ties go to annotation
// confidence, then (when no shape was asked) base blocks over their derived
// forms, then construction materials, then eligible over conditional, then
// variant ID.  Matches far weaker than the best one are dropped.
export function rankRows(rows, snapshot, query, exactQuery) {
  const { docs } = searchIndex(snapshot);
  const result = [];
  for (const row of rows) {
    const [variantId, variant, , block] = row;
    const doc = docs.get(variantId);
    const exact = exactBlockQuery(exactQuery, variant.block_id, block.official_names ?? {});
    const { score, breakdown, notes, asked } = scoreDocument(doc, query);
    if (!exact && score <= 0) continue;
    const penalized = policyWarning(variant.block_id) !== null && !exact;
    // An ore, chest or flower that only matched by colour or a material
    // word ranks below building materials; naming it asks for it.
    const demoted = doc.category !== null && !exact && !asked;
    let adjusted = penalized ? pyRound8(score * 0.25) : score;
    if (demoted) {
      adjusted = pyRound8(adjusted * NON_BUILDING_FACTOR);
      notes.push(`${doc.category} ×${NON_BUILDING_FACTOR} (not a building material)`);
    }
    result.push({
      row,
      // A full official name or block ID is a perfect match by definition.
      score: exact ? 1 : adjusted,
      relevance: score,
      breakdown,
      exact,
      penalized,
      asked,
      notes,
      confidence: doc.confidence,
      derived: query.shapes.size === 0 && SHAPE_FACTORS.has(doc.shape),
      buildingForms: doc.buildingForms,
      series: doc.series,
      oxidation: doc.oxidation,
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
  // The floor is on relevance before the list and category factors, so a
  // demoted block ranks low but is not dropped for being demoted.
  const best = Math.max(0, ...result.filter((entry) => !entry.exact).map((entry) => entry.relevance));
  return result.filter((entry) => entry.exact || entry.relevance >= RELEVANCE_FLOOR * best);
}
