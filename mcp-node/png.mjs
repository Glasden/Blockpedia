// Minimal Node port of the restricted RGBA PNG lane used by the R3 contact
// sheet: decode from src/blockpedia/png.py plus the 512px nearest resize,
// T01-style label painting and chunked encoder from src/blockpedia/r3.py.
//
// Only non-interlaced 8-bit RGBA is accepted.  Chunk framing, CRC and scanline
// length are still enforced exactly as in Python; this is not a general PNG
// decoder and deliberately no dependency is added.
import zlib from 'node:zlib';
import { createHash } from 'node:crypto';

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

function unfilter(filtered, previous, filterType, bpp) {
  const row = Buffer.from(filtered);
  if (filterType === 0) return row;
  if (filterType > 4) fail(`unsupported PNG filter ${filterType}`);
  for (let index = 0; index < row.length; index += 1) {
    const left = index >= bpp ? row[index - bpp] : 0;
    const above = previous[index];
    const upperLeft = index >= bpp ? previous[index - bpp] : 0;
    let predictor;
    if (filterType === 1) {
      predictor = left;
    } else if (filterType === 2) {
      predictor = above;
    } else if (filterType === 3) {
      predictor = (left + above) >> 1;
    } else {
      // Paeth: pick the neighbour nearest to left + above - upperLeft.
      const estimate = left + above - upperLeft;
      const distances = [Math.abs(estimate - left), Math.abs(estimate - above), Math.abs(estimate - upperLeft)];
      predictor = [left, above, upperLeft][distances.indexOf(Math.min(...distances))];
    }
    row[index] = (row[index] + predictor) & 0xff;
  }
  return row;
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
  let previous = Buffer.alloc(rowBytes);
  let cursor = 0;
  let output = 0;
  for (let row = 0; row < height; row += 1) {
    const filterType = decoded[cursor];
    cursor += 1;
    const line = unfilter(decoded.subarray(cursor, cursor + rowBytes), previous, filterType, 4);
    cursor += rowBytes;
    line.copy(pixels, output);
    output += rowBytes;
    previous = line;
  }
  return { width, height, pixels };
}

function chunk(kind, payload) {
  const out = Buffer.alloc(payload.length + 12);
  out.writeUInt32BE(payload.length, 0);
  Buffer.from(kind, 'latin1').copy(out, 4);
  payload.copy(out, 8);
  out.writeUInt32BE(crc32(Buffer.concat([Buffer.from(kind, 'latin1'), payload])), payload.length + 8);
  return out;
}

export function encodeRgbaPng(width, height, pixels) {
  if (width <= 0 || height <= 0 || pixels.length !== width * height * 4) {
    throw new Error('invalid RGBA image');
  }
  const rowBytes = width * 4;
  const rows = Buffer.alloc(height * (rowBytes + 1));
  for (let row = 0; row < height; row += 1) {
    pixels.copy(rows, row * (rowBytes + 1) + 1, row * rowBytes, (row + 1) * rowBytes);
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header.set([8, 6, 0, 0, 0], 8);
  // ponytail: Node 24 statically links zlib-ng, so the IDAT stream is not
  // byte-identical to CPython zlib 1.3 level 9; chunks, CRCs and scanlines are.
  // Wire an external stock-zlib deflate only if byte equality is required.
  return Buffer.concat([
    PNG_SIGNATURE,
    chunk('IHDR', header),
    chunk('IDAT', zlib.deflateSync(rows, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
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

function resizeNearest(image, width = 512, height = 512) {
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

function paintLabel(pixels, width, height, x, y, label) {
  const scale = 5;
  const glyphWidth = label.split('').reduce((total, char) => total + glyphFor(char)[0].length + 1, 0) * scale;
  const glyphHeight = 5 * scale;
  const left = Math.min(Math.max(0, x), Math.max(0, width - glyphWidth - 16));
  const top = Math.max(0, y);
  // Opaque backing keeps the identifier readable without a font asset; it is
  // part of the deterministic contact-sheet image.
  for (let row = 0; row < glyphHeight + 12; row += 1) {
    for (let column = 0; column < glyphWidth + 12; column += 1) {
      const px = left + column;
      const py = top + row;
      if (px >= 0 && px < width && py >= 0 && py < height) {
        pixels.set([0x18, 0x18, 0x18, 0xff], (py * width + px) * 4);
      }
    }
  }
  let cursor = left + 6;
  for (const char of label) {
    const glyph = glyphFor(char);
    for (let gy = 0; gy < glyph.length; gy += 1) {
      for (let gx = 0; gx < glyph[gy].length; gx += 1) {
        if (glyph[gy][gx] !== '1') continue;
        for (let dy = 0; dy < scale; dy += 1) {
          for (let dx = 0; dx < scale; dx += 1) {
            const px = cursor + gx * scale + dx;
            const py = top + 6 + gy * scale + dy;
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

export function makeContactSheet(images, columns = 4) {
  if (!(images.length >= 1 && images.length <= 16)) throw new Error('contact sheets contain 1-16 images');
  const cols = Math.max(1, Math.min(columns, images.length));
  const rows = Math.ceil(images.length / cols);
  const width = cols * 512;
  const height = rows * 512;
  const pixels = Buffer.alloc(width * height * 4);
  for (let index = 0; index < images.length; index += 1) {
    const card = resizeNearest(images[index]);
    const column = index % cols;
    const row = Math.floor(index / cols);
    const x0 = column * 512;
    const y0 = row * 512;
    for (let y = 0; y < 512; y += 1) {
      const target = ((y0 + y) * width + x0) * 4;
      const source = y * 512 * 4;
      card.copy(pixels, target, source, source + 512 * 4);
    }
    paintLabel(pixels, width, height, x0 + 12, y0 + 470, tileId(index));
  }
  return { png: encodeRgbaPng(width, height, pixels), width, height };
}

export function sha256Bytes(value) {
  return `sha256:${createHash('sha256').update(value).digest('hex')}`;
}
