// Preserve input order and exact aspect ratios; only complete rows expand.
export function justifiedRows(images, width, targetHeight = 220, gap = 6) {
  if (!(width > 0)) return [];
  const rows = [];
  let row = [], sum = 0;
  const ratio = image => Number(image.width) > 0 && Number(image.height) > 0
    ? Number(image.width) / Number(image.height) : 1;
  const flush = (last = false) => {
    if (!row.length) return;
    const fitted = Math.max(1, width - gap * (row.length - 1)) / sum;
    const height = last ? Math.min(targetHeight, fitted) : fitted;
    rows.push({ height, items: row.map(image => ({ image, width: ratio(image) * height })) });
    row = []; sum = 0;
  };
  for (const image of images) {
    const next = ratio(image);
    const before = row.length ? (width - gap * (row.length - 1)) / sum : Infinity;
    const after = (width - gap * row.length) / (sum + next);
    if (row.length && after < targetHeight && Math.abs(before - targetHeight) < Math.abs(after - targetHeight)) flush();
    row.push(image); sum += next;
    if ((width - gap * (row.length - 1)) / sum <= targetHeight) flush();
  }
  flush(true);
  return rows;
}
