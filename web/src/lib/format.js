// Small shared display formatters.

// Human file size: "812 KB", "54 MB", "800 MB", "1.1 GB". One decimal under 10 of a
// unit, whole numbers at/above 10, and a trailing ".0" is always dropped.
export function fmtSize(b) {
  if (b == null || b === '') return '';
  if (b < 1024) return `${b} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let n = b / 1024, u = 0;
  while (n >= 1024 && u < units.length - 1) { n /= 1024; u++; }
  const s = (n < 10 ? n.toFixed(1) : Math.round(n).toString()).replace(/\.0$/, '');
  return `${s} ${units[u]}`;
}

// Whole-number count with thousands separators: 1234 → "1,234".
export function fmtCount(n) {
  return Number(n || 0).toLocaleString();
}
