import { isMapping, pyRound8, clamp01, byUtf8 } from './text.mjs';
import { SERIES_LIMIT, NON_BUILDING_FACTOR, shapeKey, blockCategory, oxidationSeries, policyWarning } from './blocks.mjs';
import { variantsFor } from './output.mjs';
import { searchIndex } from './search.mjs';
import { paletteDistance } from '../palette.mjs';

// Palette similarity is 1 at identical colour and texture and 0 at this
// distance (Oklab plus the texture term) or beyond.
export const SIMILARITY_RANGE = 0.25;

// Folds ranked entries that share keyOf(entry) into the best-ranked one,
// placed where the group first appears; among members tied on that score the
// lowest prefer(entry) wins.  Folded entries accumulate in entry[field].  An
// exact name or ID keeps its own entry.
export const foldSeries = (ranked, field, keyOf, prefer) => {
  const keys = ranked.map((entry) => (entry.exact ? null : keyOf(entry)));
  const groups = new Map();
  ranked.forEach((entry, index) => {
    if (keys[index] !== null) groups.set(keys[index], [...(groups.get(keys[index]) ?? []), entry]);
  });
  const out = [];
  ranked.forEach((entry, index) => {
    const members = keys[index] === null ? undefined : groups.get(keys[index]);
    if (members === undefined || members.length === 1) {
      out.push(entry);
      return;
    }
    if (members[0] !== entry) return;
    const best = members.filter((member) => member.score === entry.score).sort((left, right) => prefer(left) - prefer(right))[0];
    const folded = members.filter((member) => member !== best).flatMap((member) => [member, ...(member[field] ?? [])]);
    out.push({ ...best, [field]: [...(best[field] ?? []), ...folded] });
  });
  return out;
};
// Keeps the first SERIES_LIMIT entries of each look series in rank order; the
// rest are listed on the series' first kept entry as seriesOmitted.
export const capSeries = (ranked) => {
  const kept = new Map();
  const out = [];
  for (const entry of ranked) {
    const members = entry.lookSeries === null ? undefined : kept.get(entry.lookSeries.pattern);
    if (members === undefined || members.length < SERIES_LIMIT) {
      const copy = { ...entry };
      if (entry.lookSeries !== null) kept.set(entry.lookSeries.pattern, [...(members ?? []), copy]);
      out.push(copy);
    } else {
      members[0].seriesOmitted = [...(members[0].seriesOmitted ?? []), entry];
    }
  }
  return out;
};

// Blocks of the same shape class as `targetId`, nearest palette first.  With
// keywords, `textRanked` (their ranked matches) is the pool and the score is
// still the palette similarity alone.  Null when the target has no preview
// or no visible face.  Recommendation-list blocks keep the ×0.25 penalty,
// so infested stone does not lead a search for stone; ores, containers,
// functional blocks and plants rank ×0.5 unless the target is of the same
// kind or the keywords name them.
export function rankSimilar(service, handle, snapshot, targetId, textRanked, resources) {
  const target = variantsFor(snapshot, targetId)[0];
  if (target === undefined || !isMapping(target.render)) return null;
  const reference = service._palette(handle, snapshot, String(target.variant_id), resources);
  if (reference.top === null && reference.side === null) return null;
  const targetShape = shapeKey(targetId, snapshot.blocks[targetId].tags ?? [], target.machine_facts?.geometry);
  const targetCategory = blockCategory(targetId, snapshot.blocks[targetId].tags ?? []);
  const { docs } = searchIndex(snapshot);
  const pool = textRanked ?? service._eligibleRows(snapshot).map((row) => ({ row, breakdown: { text: 0 }, asked: false }));
  const result = [];
  const targetOxidation = oxidationSeries(targetId, new Set(Object.keys(snapshot.blocks)));
  for (const { row, breakdown, asked } of pool) {
    const [variantId, variant, , block] = row;
    if (variant.block_id === targetId) continue;
    // The target's waxed or unwaxed twin looks exactly like it.
    const { category, oxidation, lookSeries: series } = docs.get(variantId);
    if (targetOxidation !== null && oxidation?.key === targetOxidation.key && oxidation.stage === targetOxidation.stage) continue;
    if (shapeKey(String(variant.block_id), block.tags ?? [], variant.machine_facts?.geometry) !== targetShape) continue;
    const distance = paletteDistance(reference, service._palette(handle, snapshot, variantId, resources));
    if (distance === null) continue;
    const similarity = pyRound8(clamp01(1 - distance.distance / SIMILARITY_RANGE));
    if (similarity <= 0) continue;
    const penalized = policyWarning(variant.block_id) !== null;
    const demoted = category !== null && category !== targetCategory && !asked;
    let score = penalized ? pyRound8(similarity * 0.25) : similarity;
    if (demoted) score = pyRound8(score * NON_BUILDING_FACTOR);
    result.push({
      row,
      score,
      // The shape class always matches, so shape is not a dimension here.
      breakdown: { text: breakdown.text, color: similarity },
      exact: false,
      penalized,
      oxidation,
      lookSeries: series,
      // The response's similarity.basis explains these terms once.
      notes: [
        `ΔE ${(distance.color * 100).toFixed(1)}, texture ${(distance.texture * 100).toFixed(1)}`,
        ...(demoted ? [`${category} ×${NON_BUILDING_FACTOR} (not a building material)`] : []),
      ],
      distance: distance.distance,
    });
  }
  result.sort((left, right) => right.score - left.score
    || left.distance - right.distance
    || byUtf8(left.row[0], right.row[0]));
  return { ranked: result, shapeClass: targetShape };
}
