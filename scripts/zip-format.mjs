import { deflateRawSync, constants as zlibConstants } from 'node:zlib';

const UTF8_FLAG = 0x0800;
const DEFLATE_METHOD = 8;
const DOS_TIME = 0;
const DOS_DATE = 0x0021;
const ZIP32_MAX_ENTRIES = 0xffff;
const ZIP32_MAX_VALUE = 0xffffffff;

function requireZip32(condition, detail) {
  if (!condition) throw new Error(`ZIP64 is not supported: ${detail}`);
}

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function validateName(name) {
  if (typeof name !== 'string' || name.length === 0) throw new TypeError('entry name must be a nonempty string');
  if (name.startsWith('/') || name.includes('\\')) throw new Error(`invalid relative POSIX file name: ${name}`);
  const segments = name.split('/');
  if (segments.some((segment) => segment === '' || segment === '.' || segment === '..')) {
    throw new Error(`invalid relative POSIX file name: ${name}`);
  }
  const encoded = Buffer.from(name, 'utf8');
  if (encoded.toString('utf8') !== name) throw new Error(`file name is not valid Unicode: ${name}`);
  requireZip32(encoded.length <= 0xffff, `file name is too long: ${name}`);
  return encoded;
}

function localHeader({ name, crc, compressedSize, uncompressedSize }) {
  const header = Buffer.alloc(30);
  header.writeUInt32LE(0x04034b50, 0);
  header.writeUInt16LE(20, 4);
  header.writeUInt16LE(UTF8_FLAG, 6);
  header.writeUInt16LE(DEFLATE_METHOD, 8);
  header.writeUInt16LE(DOS_TIME, 10);
  header.writeUInt16LE(DOS_DATE, 12);
  header.writeUInt32LE(crc, 14);
  header.writeUInt32LE(compressedSize, 18);
  header.writeUInt32LE(uncompressedSize, 22);
  header.writeUInt16LE(name.length, 26);
  header.writeUInt16LE(0, 28);
  return header;
}

function centralHeader({ name, crc, compressedSize, uncompressedSize, offset, mode }) {
  const header = Buffer.alloc(46);
  header.writeUInt32LE(0x02014b50, 0);
  header.writeUInt16LE(0x0314, 4);
  header.writeUInt16LE(20, 6);
  header.writeUInt16LE(UTF8_FLAG, 8);
  header.writeUInt16LE(DEFLATE_METHOD, 10);
  header.writeUInt16LE(DOS_TIME, 12);
  header.writeUInt16LE(DOS_DATE, 14);
  header.writeUInt32LE(crc, 16);
  header.writeUInt32LE(compressedSize, 20);
  header.writeUInt32LE(uncompressedSize, 24);
  header.writeUInt16LE(name.length, 28);
  header.writeUInt16LE(0, 30);
  header.writeUInt16LE(0, 32);
  header.writeUInt16LE(0, 34);
  header.writeUInt16LE(0, 36);
  header.writeUInt32LE(((0o100000 | mode) << 16) >>> 0, 38);
  header.writeUInt32LE(offset, 42);
  return header;
}

export function encodeZip(entries) {
  if (!Array.isArray(entries)) throw new TypeError('entries must be an array');
  requireZip32(entries.length < ZIP32_MAX_ENTRIES, `entry count reaches ZIP64 sentinel ${ZIP32_MAX_ENTRIES}`);

  const seen = new Set();
  const normalized = entries.map((entry) => {
    if (!entry || typeof entry !== 'object') throw new TypeError('each entry must be an object');
    const name = validateName(entry.name);
    const key = name.toString('hex');
    if (seen.has(key)) throw new Error(`duplicate file name: ${entry.name}`);
    seen.add(key);
    if (!Buffer.isBuffer(entry.content)) throw new TypeError(`content must be a Buffer: ${entry.name}`);
    const mode = entry.mode === '100644' ? 0o644 : entry.mode === '100755' ? 0o755 : null;
    if (mode === null) throw new Error(`unsupported file mode: ${entry.mode}`);
    return { name, content: entry.content, mode };
  }).sort((left, right) => Buffer.compare(left.name, right.name));

  const localParts = [];
  const centralParts = [];
  let offset = 0;
  for (const entry of normalized) {
    const compressed = deflateRawSync(entry.content, {
      level: 9,
      memLevel: 9,
      strategy: zlibConstants.Z_FIXED,
    });
    requireZip32(entry.content.length < ZIP32_MAX_VALUE, 'uncompressed size reaches ZIP32 sentinel');
    requireZip32(compressed.length < ZIP32_MAX_VALUE, 'compressed size reaches ZIP32 sentinel');
    requireZip32(offset < ZIP32_MAX_VALUE, 'local header offset reaches ZIP32 sentinel');
    const crc = crc32(entry.content);
    localParts.push(localHeader({ name: entry.name, crc, compressedSize: compressed.length, uncompressedSize: entry.content.length }), entry.name, compressed);
    centralParts.push(centralHeader({
      name: entry.name,
      crc,
      compressedSize: compressed.length,
      uncompressedSize: entry.content.length,
      offset,
      mode: entry.mode,
    }), entry.name);
    offset += 30 + entry.name.length + compressed.length;
    requireZip32(offset < ZIP32_MAX_VALUE, 'central directory offset reaches ZIP32 sentinel');
  }

  const centralOffset = offset;
  const centralSize = centralParts.reduce((total, part) => total + part.length, 0);
  requireZip32(centralSize < ZIP32_MAX_VALUE, 'central directory size reaches ZIP32 sentinel');
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(0, 4);
  end.writeUInt16LE(0, 6);
  end.writeUInt16LE(normalized.length, 8);
  end.writeUInt16LE(normalized.length, 10);
  end.writeUInt32LE(centralSize, 12);
  end.writeUInt32LE(centralOffset, 16);
  end.writeUInt16LE(0, 20);
  return Buffer.concat([...localParts, ...centralParts, end]);
}
