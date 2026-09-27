// Face colours of a block from its release preview, derived at query time.
//
// The exporter's 512px preview holds four 256px orthographic views
// (RenderExporter.View): isometric top-left, front (north face) top-right,
// side (east face) bottom-left and top bottom-right.  lighting.v2 shades each
// face by a fixed factor of its texture colour (measured in D1: top 1.0, north
// 0.74, east 0.497), so dividing it back gives texture colour for building
// palettes.  The isometric view mixes faces and is not used.  Faces tilted off
// the axes (crossed plants) only approximate this.

const FACE_VIEWS = {
  top: [{ column: 1, row: 1, shade: 1.0 }],
  side: [{ column: 1, row: 0, shade: 0.74 }, { column: 0, row: 1, shade: 0.497 }],
};
// Textures are 16px scaled to 256px per view, so every second pixel still
// samples each texel many times over.
const STEP = 2;
const MIN_SAMPLES = 16;
// Dominant colours: Oklab bins merged greedily, largest first, into clusters
// no wider than this radius; up to three with at least 10 % of the face.
const BIN = [0.03, 0.02, 0.02];
const CLUSTER_RADIUS = 0.06;
const MIN_SHARE = 0.1;
const MAX_DOMINANT = 3;

const toLinear = (value) => (value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
const toSrgb8 = (value) => {
  const clamped = Math.min(1, Math.max(0, value));
  return Math.round(255 * (clamped <= 0.0031308 ? 12.92 * clamped : 1.055 * clamped ** (1 / 2.4) - 0.055));
};
const lightness = (y) => (y > 216 / 24389 ? 116 * Math.cbrt(y) - 16 : (24389 / 27) * y);

// Preview pixels are alpha-premultiplied (white stained glass is stored as
// 102,102,102,102), so a channel is divided by its alpha and then by the
// face's shading to get linear texture colour.
const textureLinear = (value, alpha, shade) => toLinear(Math.min(255, (value * 255) / alpha / shade) / 255);

export const linearToOklab = (r, g, b) => {
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ];
};

const oklabToLinear = ([L, a, b]) => {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ];
};

const hex = (oklab) => `#${oklabToLinear(oklab).map((value) => toSrgb8(value).toString(16).padStart(2, '0')).join('')}`;
const lightnessOf = (oklab) => {
  const [r, g, b] = oklabToLinear(oklab).map((value) => Math.min(1, Math.max(0, value)));
  return lightness(0.2126729 * r + 0.7151522 * g + 0.072175 * b);
};
export const oklabDistance = (left, right) => Math.hypot(left[0] - right[0], left[1] - right[1], left[2] - right[2]);
const round = (value, digits) => Number(value.toFixed(digits));

function faceStats(image, views) {
  const quadrant = image.width / 2;
  const { pixels, width } = image;
  let weight = 0;
  let samples = 0;
  const sum = [0, 0, 0];
  let lSum = 0;
  let lSquares = 0;
  const bins = new Map();
  for (const { column, row, shade } of views) {
    // A face shows a few hundred distinct texel colours at most: count them
    // first and convert each once.
    const counts = new Map();
    for (let y = row * quadrant; y < (row + 1) * quadrant; y += STEP) {
      for (let x = column * quadrant; x < (column + 1) * quadrant; x += STEP) {
        const index = (y * width + x) * 4;
        if (pixels[index + 3] === 0) continue;
        const packed = pixels.readUInt32BE(index);
        counts.set(packed, (counts.get(packed) ?? 0) + 1);
      }
    }
    for (const [packed, count] of counts) {
      // Translucent pixels (stained glass) count by their opacity.
      const alpha = packed & 0xff;
      const w = (count * alpha) / 255;
      const r = textureLinear(packed >>> 24, alpha, shade);
      const g = textureLinear((packed >>> 16) & 0xff, alpha, shade);
      const b = textureLinear((packed >>> 8) & 0xff, alpha, shade);
      const lab = linearToOklab(r, g, b);
      const l = lightness(0.2126729 * r + 0.7151522 * g + 0.072175 * b);
      samples += count;
      weight += w;
      lSum += w * l;
      lSquares += w * l * l;
      for (let channel = 0; channel < 3; channel += 1) sum[channel] += w * lab[channel];
      const key = lab.map((value, channel) => Math.round(value / BIN[channel])).join(',');
      const bin = bins.get(key);
      if (bin === undefined) bins.set(key, { key, weight: w, sum: lab.map((value) => w * value) });
      else {
        bin.weight += w;
        for (let channel = 0; channel < 3; channel += 1) bin.sum[channel] += w * lab[channel];
      }
    }
  }
  if (samples < MIN_SAMPLES) return null;
  const mean = sum.map((value) => value / weight);
  const meanL = lSum / weight;
  const clusters = [];
  const ordered = [...bins.values()].sort((left, right) => right.weight - left.weight || (left.key < right.key ? -1 : 1));
  for (const bin of ordered) {
    const center = bin.sum.map((value) => value / bin.weight);
    let nearest = null;
    let best = CLUSTER_RADIUS;
    for (const cluster of clusters) {
      const distance = oklabDistance(center, cluster.sum.map((value) => value / cluster.weight));
      if (distance <= best) {
        best = distance;
        nearest = cluster;
      }
    }
    if (nearest === null) clusters.push({ weight: bin.weight, sum: [...bin.sum] });
    else {
      nearest.weight += bin.weight;
      for (let channel = 0; channel < 3; channel += 1) nearest.sum[channel] += bin.sum[channel];
    }
  }
  clusters.sort((left, right) => right.weight - left.weight);
  const dominant = clusters
    .filter((cluster, index) => index === 0 || cluster.weight / weight >= MIN_SHARE)
    .slice(0, MAX_DOMINANT)
    .map((cluster) => ({ hex: hex(cluster.sum.map((value) => value / cluster.weight)), share: round(cluster.weight / weight, 2) }));
  return {
    oklab: mean,
    lightnessStd: Math.sqrt(Math.max(0, lSquares / weight - meanL * meanL)),
    output: {
      hex: hex(mean),
      lightness: round(lightnessOf(mean), 1),
      // Spread of per-pixel CIELAB L*: near 0 for concrete, 10+ for stone,
      // 25+ for cobblestone.
      lightness_std: round(Math.sqrt(Math.max(0, lSquares / weight - meanL * meanL)), 1),
      dominant,
    },
  };
}

// { top, side } of face stats, each null when that face shows too few pixels
// (a torch seen from above).  `oklab` is the mean for distance; `output` is
// the MCP projection.
export function facePalette(image) {
  return Object.fromEntries(Object.entries(FACE_VIEWS).map(([face, views]) => [face, faceStats(image, views)]));
}

export const paletteOutput = (palette) => (palette.top === null && palette.side === null
  ? null
  : { top: palette.top?.output ?? null, side: palette.side?.output ?? null });

// Stone and cobblestone share a mean grey; their L* spread (4 vs 11) is what
// tells them apart, so each point of spread difference adds this much to the
// Oklab distance.
const TEXTURE_WEIGHT = 0.005;

// Mean over the faces both blocks show of Oklab distance plus the texture
// term; null when they share no face.  `color` and `texture` are the two parts.
export function paletteDistance(left, right) {
  const faces = ['top', 'side'].filter((face) => left[face] !== null && right[face] !== null);
  if (faces.length === 0) return null;
  let color = 0;
  let texture = 0;
  for (const face of faces) {
    color += oklabDistance(left[face].oklab, right[face].oklab) / faces.length;
    texture += (TEXTURE_WEIGHT * Math.abs(left[face].lightnessStd - right[face].lightnessStd)) / faces.length;
  }
  return { distance: color + texture, color, texture };
}
