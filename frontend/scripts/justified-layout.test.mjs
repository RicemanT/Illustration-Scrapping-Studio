import test from 'node:test';
import assert from 'node:assert/strict';
import { justifiedRows } from '../src/components/justifiedLayout.js';

test('mixed proportions fit complete rows without cropping or changing order', () => {
  const images = Array.from({ length: 60 }, (_, id) => ({ id, width: [600, 1600, 1000][id % 3], height: 1000 }));
  for (const width of [280, 768, 1440]) {
    const rows = justifiedRows(images, width);
    assert.deepEqual(rows.flatMap(row => row.items.map(item => item.image.id)), images.map(image => image.id));
    rows.forEach((row, index) => {
      const occupied = row.items.reduce((sum, item) => sum + item.width, 0) + (row.items.length - 1) * 6;
      assert.ok(occupied <= width + 0.001);
      if (index < rows.length - 1) assert.ok(Math.abs(occupied - width) < 0.001);
      for (const item of row.items) assert.ok(Math.abs(item.width / row.height - item.image.width / item.image.height) < 0.001);
    });
  }
});

test('last row stays compact, extreme ratios fit, and unknown dimensions are safe', () => {
  assert.equal(justifiedRows([{ width: 500, height: 1000 }], 1200)[0].height, 220);
  const wide = justifiedRows([{ width: 10000, height: 100 }], 400)[0];
  assert.equal(wide.items[0].width, 400);
  assert.equal(wide.height, 4);
  assert.equal(justifiedRows([{}], 400)[0].items[0].width, 220);
  assert.deepEqual(justifiedRows([], 400), []);
  assert.deepEqual(justifiedRows([{}], 0), []);
});
