// Flickr-style justified layout: pack items into full-width rows where every
// row is close to a target height, each row scaled so its items exactly fill the
// container width. Items carry aspect ratios; the last (partial) row is left at
// target height rather than stretched.

export function justify(items, containerWidth, targetHeight, gap) {
  const rows = [];
  if (!containerWidth || containerWidth <= 0) return rows;
  let buf = [];
  let arSum = 0;

  const flush = (stretch) => {
    if (!buf.length) return;
    const totalGap = gap * (buf.length - 1);
    const height = stretch
      ? (containerWidth - totalGap) / arSum
      : targetHeight;
    const cells = buf.map((b) => ({ item: b.item, w: b.ar * height, h: height }));
    rows.push({ height, cells, stretched: stretch });
    buf = [];
    arSum = 0;
  };

  for (const item of items) {
    const ar = item.thumb_w && item.thumb_h ? item.thumb_w / item.thumb_h : 1.5;
    // Clamp extreme ratios so one panorama can't dominate a row.
    const clamped = Math.max(0.4, Math.min(3.2, ar));
    buf.push({ item, ar: clamped });
    arSum += clamped;
    const rowWidth = arSum * targetHeight + gap * (buf.length - 1);
    if (rowWidth >= containerWidth) flush(true);
  }
  flush(false);
  return rows;
}
