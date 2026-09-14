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

// A stored timestamp in the viewer's own time zone: "Jun 1, 2026, 12:09 PM CDT".
// created_at is ISO-8601 UTC ("…T17:09:36.640727Z"); the fraction is trimmed to ms for
// strict parsers. Date-only values ("2026-06-01") are shown as-is — parsing one reads as
// UTC midnight and slides back a day west of Greenwich. Unparseable values pass through.
export function fmtDateTime(v) {
  if (v == null || v === '') return '';
  const s = String(v);
  let d;
  if (/^\d+(\.\d+)?$/.test(s)) d = new Date(Number(s) < 1e12 ? Number(s) * 1000 : Number(s));
  else if (s.includes('T')) d = new Date(s.replace(/(\.\d{3})\d+/, '$1'));
  else return s;
  if (isNaN(d)) return s;
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit', timeZoneName: 'short'
  });
}

// Whole-number count with thousands separators: 1234 → "1,234".
export function fmtCount(n) {
  return Number(n || 0).toLocaleString();
}
