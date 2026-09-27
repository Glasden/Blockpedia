// Lossless WebP encoding for the final MCP image responses.
//
// Uses the libwebp WebAssembly build shipped by @jsquash/webp, so Windows and
// Linux share one portable encoder with no native binary.  The plain (non-SIMD)
// module is loaded explicitly from disk: the package's own loader fetches the
// wasm by URL, which Node's fetch cannot serve from file:.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const glue = require.resolve('@jsquash/webp/codec/enc/webp_enc.js');
const { default: factory } = await import(glue);
const { defaultOptions } = await import(require.resolve('@jsquash/webp/meta.js'));
const wasm = new WebAssembly.Module(readFileSync(glue.replace(/\.js$/, '.wasm')));
const encoder = await factory({
  noInitialRun: true,
  instantiateWasm: (imports, callback) => {
    const instance = new WebAssembly.Instance(wasm, imports);
    callback(instance);
    return instance.exports;
  },
});

// libwebp's default lossless effort (cwebp -lossless: q=75, m=4).  q=100/m=6
// saved almost nothing on real previews but cost ~100x the encode time.
// `exact` keeps RGB under fully transparent pixels, so decoding returns the
// resampled RGBA bytes unchanged.
const OPTIONS = { ...defaultOptions, lossless: 1, quality: 75, method: 4, exact: 1 };

export function encodeLosslessWebp(width, height, pixels) {
  if (width <= 0 || height <= 0 || pixels.length !== width * height * 4) {
    throw new Error('invalid RGBA image');
  }
  const result = encoder.encode(new Uint8ClampedArray(pixels.buffer, pixels.byteOffset, pixels.length), width, height, OPTIONS);
  if (!result) throw new Error('WebP encoding failed');
  return Buffer.from(result); // Copy out of the wasm heap before the next call.
}
