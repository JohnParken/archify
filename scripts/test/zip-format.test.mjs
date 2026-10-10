import test from 'node:test';
import assert from 'node:assert/strict';
import { encodeZip } from '../zip-format.mjs';

function centralEntries(archive) {
  const entries = [];
  let offset = archive.readUInt32LE(archive.length - 6);
  const count = archive.readUInt16LE(archive.length - 12);
  for (let index = 0; index < count; index += 1) {
    assert.equal(archive.readUInt32LE(offset), 0x02014b50);
    const nameLength = archive.readUInt16LE(offset + 28);
    const extraLength = archive.readUInt16LE(offset + 30);
    const commentLength = archive.readUInt16LE(offset + 32);
    entries.push({
      name: archive.subarray(offset + 46, offset + 46 + nameLength).toString('utf8'),
      crc: archive.readUInt32LE(offset + 16),
      mode: archive.readUInt32LE(offset + 38) >>> 16,
      date: archive.readUInt16LE(offset + 14),
      time: archive.readUInt16LE(offset + 12),
    });
    offset += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
}

test('encoding sorts entries and is independent of input ordering', () => {
  const a = { name: 'z.txt', content: Buffer.from('last'), mode: '100644' };
  const b = { name: 'a.txt', content: Buffer.from('first'), mode: '100755' };
  const first = encodeZip([a, b]);
  const second = encodeZip([b, a]);
  assert.deepEqual(first, second);
  assert.deepEqual(centralEntries(first).map((entry) => entry.name), ['a.txt', 'z.txt']);
});

test('headers record CRC, Unix modes, and the fixed DOS date', () => {
  const archive = encodeZip([
    { name: 'plain', content: Buffer.from('payload'), mode: '100644' },
    { name: 'exec', content: Buffer.from('run'), mode: '100755' },
  ]);
  const entries = centralEntries(archive);
  assert.deepEqual(entries.map(({ name, mode, date, time }) => ({ name, mode, date, time })), [
    { name: 'exec', mode: 0o100755, date: 0x21, time: 0 },
    { name: 'plain', mode: 0o100644, date: 0x21, time: 0 },
  ]);
  assert.equal(entries[0].crc, 0x5076a4c0);
  assert.equal(entries[1].crc, 0x422c6a15);
});

test('rejects duplicate names, unsafe names, and unknown modes', () => {
  const file = (name, mode = '100644') => ({ name, content: Buffer.alloc(0), mode });
  assert.throws(() => encodeZip([file('same'), file('same')]), /duplicate/);
  for (const name of ['', '/absolute', '../escape', 'a/../b', './file', 'a\\b', 'a//b', 'dir/']) {
    assert.throws(() => encodeZip([file(name)]), /name/);
  }
  assert.throws(() => encodeZip([file('file', '100600')]), /mode/);
});
