// Minimal Node port of the restricted RGBA PNG decoder from
// src/blockpedia/png.py, plus the MCP image composition: nearest resampling to
// 256px block cards, T01-style labels and one lossless WebP per response.
//
// Only non-interlaced 8-bit RGBA is accepted.  Chunk framing, CRC and scanline
// length are still enforced exactly as in Python; this is not a general PNG
// decoder and deliberately no dependency is added.  Release previews are read
// unchanged; only the bytes sent to the MCP client are resampled and re-encoded.
import zlib from 'node:zlib';
import { encodeLosslessWebp } from './webp.mjs';

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

export class PngDecodeError extends Error {
  constructor(message) {
    super(message);
    this.name = 'PngDecodeError';
  }
}

const fail = (message) => {
  throw new PngDecodeError(message);
};

const crc32 = (bytes) => zlib.crc32(bytes) >>> 0;

function parseChunks(source) {
  const raw = Buffer.isBuffer(source) ? source : Buffer.from(source);
  if (raw.length < 8 || !raw.subarray(0, 8).equals(PNG_SIGNATURE)) fail('PNG signature invalid');
  let offset = 8;
  let header = null;
  const idat = [];
  let sawIend = false;
  while (offset + 12 <= raw.length) {
    const length = raw.readUInt32BE(offset);
    const start = offset + 8;
    const end = start + length;
    if (end + 4 > raw.length) fail('PNG chunk truncated');
    const kind = raw.subarray(offset + 4, offset + 8);
    const data = raw.subarray(start, end);
    const crc = raw.readUInt32BE(end);
    if (crc32(Buffer.concat([kind, data])) !== crc) fail('PNG CRC mismatch');
    const name = kind.toString('latin1');
    if (name === 'IHDR') {
      if (data.length !== 13) fail('PNG IHDR invalid');
      header = {
        width: data.readUInt32BE(0),
        height: data.readUInt32BE(4),
        depth: data[8],
        colorType: data[9],
        compression: data[10],
        filterMethod: data[11],
        interlace: data[12],
      };
    } else if (name === 'IDAT') {
      idat.push(data);
    } else if (name === 'IEND') {
      sawIend = true;
      break;
    }
    offset = end + 4;
  }
  if (header === null || idat.length === 0 || !sawIend) fail('PNG missing required chunks');
  return { header, idat };
}

// Reverses one scanline's filter in place: `src` is the filtered row, the
// result goes to pixels[out..], and the reconstructed previous row is read
// from the same buffer.  One loop per filter type keeps the hot loop free of
// branches; a release decode runs about a billion of these byte steps.
function unfilterInto(pixels, out, src, rowBytes, filterType, bpp) {
  const up = out - rowBytes;
  const first = out === 0;
  if (filterType === 0 || (filterType === 2 && first)) {
    pixels.set(src, out);
  } else if (filterType === 1 || (filterType === 4 && first)) {
    // Paeth with no row above reduces to Sub.
    for (let i = 0; i < bpp; i += 1) pixels[out + i] = src[i];
    for (let i = bpp; i < rowBytes; i += 1) pixels[out + i] = (src[i] + pixels[out + i - bpp]) & 0xff;
  } else if (filterType === 2) {
    for (let i = 0; i < rowBytes; i += 1) pixels[out + i] = (src[i] + pixels[up + i]) & 0xff;
  } else if (filterType === 3) {
    for (let i = 0; i < rowBytes; i += 1) {
      const left = i >= bpp ? pixels[out + i - bpp] : 0;
      pixels[out + i] = (src[i] + ((left + (first ? 0 : pixels[up + i])) >> 1)) & 0xff;
    }
  } else {
    for (let i = 0; i < bpp; i += 1) pixels[out + i] = (src[i] + pixels[up + i]) & 0xff;
    for (let i = bpp; i < rowBytes; i += 1) {
      // Paeth: the neighbour nearest to left + above - upperLeft; ties prefer
      // left, then above.
      const a = pixels[out + i - bpp];
      const b = pixels[up + i];
      const c = pixels[up + i - bpp];
      const pa = Math.abs(b - c);
      const pb = Math.abs(a - c);
      const pc = Math.abs(a + b - 2 * c);
      pixels[out + i] = (src[i] + (pa <= pb && pa <= pc ? a : pb <= pc ? b : c)) & 0xff;
    }
  }
}

export function decodeRgbaPng(source) {
  const { header, idat } = parseChunks(source);
  const { width, height } = header;
  if (
    width <= 0 ||
    height <= 0 ||
    header.depth !== 8 ||
    header.colorType !== 6 ||
    header.compression !== 0 ||
    header.filterMethod !== 0 ||
    header.interlace !== 0
  ) {
    fail('only non-interlaced 8-bit RGBA PNG is supported');
  }
  let decoded;
  try {
    decoded = zlib.inflateSync(Buffer.concat(idat));
  } catch {
    fail('PNG image data invalid');
  }
  const rowBytes = width * 4;
  if (decoded.length !== height * (rowBytes + 1)) fail('PNG scanline length mismatch');
  for (let row = 0; row < height; row += 1) {
    if (decoded[row * (rowBytes + 1)] > 4) fail(`unsupported PNG filter ${decoded[row * (rowBytes + 1)]}`);
  }
  const pixels = Buffer.alloc(width * height * 4);
  const view = new Uint8Array(pixels.buffer, pixels.byteOffset, pixels.length);
  const scanlines = new Uint8Array(decoded.buffer, decoded.byteOffset, decoded.length);
  let cursor = 0;
  for (let output = 0; output < pixels.length; output += rowBytes) {
    const filterType = scanlines[cursor];
    unfilterInto(view, output, scanlines.subarray(cursor + 1, cursor + 1 + rowBytes), rowBytes, filterType, 4);
    cursor += rowBytes + 1;
  }
  return { width, height, pixels };
}

const GLYPHS = {
  0: ['111', '101', '101', '101', '111'],
  1: ['010', '110', '010', '010', '111'],
  2: ['111', '001', '111', '100', '111'],
  3: ['111', '001', '111', '001', '111'],
  4: ['101', '101', '111', '001', '001'],
  5: ['111', '100', '111', '001', '111'],
  6: ['111', '100', '111', '101', '111'],
  7: ['111', '001', '001', '001', '001'],
  8: ['111', '101', '111', '101', '111'],
  9: ['111', '101', '111', '001', '111'],
  T: ['111', '010', '010', '010', '010'],
};

const glyphFor = (char) => GLYPHS[char] ?? GLYPHS['0'];
const tileId = (index) => `T${String(index + 1).padStart(2, '0')}`;

// Each block card is 256x256: the 512px four-view preview becomes four 128px
// views.  Contact sheets place one card per block instead of shrinking the
// whole sheet to 256px.
export const CARD_SIZE = 256;
const LABEL_SCALE = 3;
const LABEL_PAD = 4;

function resizeNearest(image, width = CARD_SIZE, height = CARD_SIZE) {
  if (image.width === width && image.height === height) return image.pixels;
  const result = Buffer.alloc(width * height * 4);
  for (let y = 0; y < height; y += 1) {
    const sourceY = Math.min(image.height - 1, Math.floor((y * image.height) / height));
    for (let x = 0; x < width; x += 1) {
      const sourceX = Math.min(image.width - 1, Math.floor((x * image.width) / width));
      const source = (sourceY * image.width + sourceX) * 4;
      image.pixels.copy(result, (y * width + x) * 4, source, source + 4);
    }
  }
  return result;
}

function paintLabel(pixels, width, height, x, y, label, scale = LABEL_SCALE, pad = LABEL_PAD) {
  const glyphWidth = label.split('').reduce((total, char) => total + glyphFor(char)[0].length + 1, 0) * scale;
  const glyphHeight = 5 * scale;
  const left = Math.min(Math.max(0, x), Math.max(0, width - glyphWidth - 2 * pad));
  const top = Math.max(0, y);
  // Opaque backing keeps the identifier readable without a font asset; it is
  // part of the deterministic contact-sheet image.
  for (let row = 0; row < glyphHeight + 2 * pad; row += 1) {
    for (let column = 0; column < glyphWidth + 2 * pad; column += 1) {
      const px = left + column;
      const py = top + row;
      if (px >= 0 && px < width && py >= 0 && py < height) {
        pixels.set([0x18, 0x18, 0x18, 0xff], (py * width + px) * 4);
      }
    }
  }
  let cursor = left + pad;
  for (const char of label) {
    const glyph = glyphFor(char);
    for (let gy = 0; gy < glyph.length; gy += 1) {
      for (let gx = 0; gx < glyph[gy].length; gx += 1) {
        if (glyph[gy][gx] !== '1') continue;
        for (let dy = 0; dy < scale; dy += 1) {
          for (let dx = 0; dx < scale; dx += 1) {
            const px = cursor + gx * scale + dx;
            const py = top + pad + gy * scale + dy;
            if (px >= 0 && px < width && py >= 0 && py < height) {
              pixels.set([0xff, 0xff, 0xff, 0xff], (py * width + px) * 4);
            }
          }
        }
      }
    }
    cursor += (glyph[0].length + 1) * scale;
  }
}

export function makeBlockCard(image) {
  return { webp: encodeLosslessWebp(CARD_SIZE, CARD_SIZE, resizeNearest(image)), width: CARD_SIZE, height: CARD_SIZE };
}

// Contact-sheet tile layouts.  full: the whole 256px four-view card.
// compact: only the isometric (top-left) and top (bottom-right) views at 64px
// side by side, for choosing among candidates at a quarter of the pixels.
export const SHEET_LAYOUTS = {
  full: { width: CARD_SIZE, height: CARD_SIZE, views: [[0, 0]], view: CARD_SIZE, label: { scale: LABEL_SCALE, pad: LABEL_PAD, left: 6, bottom: 3 } },
  compact: { width: 128, height: 64, views: [[0, 0], [1, 1]], view: 64, label: { scale: 2, pad: 2, left: 1, bottom: 1 } },
};

// One view of the 2x2 preview (or, with a single [0, 0] view, the whole
// preview) nearest-resampled to size x size.
function viewNearest(image, [column, row], size, whole) {
  if (whole) return resizeNearest(image, size, size);
  const half = { width: image.width / 2, height: image.height / 2 };
  const result = Buffer.alloc(size * size * 4);
  for (let y = 0; y < size; y += 1) {
    const sourceY = row * half.height + Math.min(half.height - 1, Math.floor((y * half.height) / size));
    for (let x = 0; x < size; x += 1) {
      const sourceX = column * half.width + Math.min(half.width - 1, Math.floor((x * half.width) / size));
      const source = (sourceY * image.width + sourceX) * 4;
      image.pixels.copy(result, (y * size + x) * 4, source, source + 4);
    }
  }
  return result;
}

export function makeContactSheet(images, columns = 4, layoutName = 'full') {
  if (!(images.length >= 1 && images.length <= 16)) throw new Error('contact sheets contain 1-16 images');
  const layout = SHEET_LAYOUTS[layoutName];
  const cols = Math.max(1, Math.min(columns, images.length));
  const rows = Math.ceil(images.length / cols);
  const width = cols * layout.width;
  const height = rows * layout.height;
  const { label } = layout;
  const labelHeight = 5 * label.scale + 2 * label.pad;
  const pixels = Buffer.alloc(width * height * 4);
  for (let index = 0; index < images.length; index += 1) {
    const x0 = (index % cols) * layout.width;
    const y0 = Math.floor(index / cols) * layout.height;
    layout.views.forEach((view, slot) => {
      const tile = viewNearest(images[index], view, layout.view, layout.views.length === 1);
      for (let y = 0; y < layout.view; y += 1) {
        const target = ((y0 + y) * width + x0 + slot * layout.view) * 4;
        tile.copy(pixels, target, y * layout.view * 4, (y + 1) * layout.view * 4);
      }
    });
    paintLabel(pixels, width, height, x0 + label.left, y0 + layout.height - labelHeight - label.bottom, tileId(index), label.scale, label.pad);
  }
  // One encode for the finished sheet, not one per card.
  return { webp: encodeLosslessWebp(width, height, pixels), width, height };
}
