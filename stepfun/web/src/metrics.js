// 作者：FDE WorkMate
// 检索质量与统计指标。

export function recallAt(hits, relevant, k) {
  const rel = new Set(relevant);
  if (!rel.size) return 0;
  return hits.slice(0, k).filter((id) => rel.has(id)).length / rel.size;
}

export function ndcgAt(hits, relevant, k) {
  const rel = new Set(relevant);
  let dcg = 0;
  hits.slice(0, k).forEach((id, i) => {
    if (rel.has(id)) dcg += 1 / Math.log2(i + 2);
  });
  let idcg = 0;
  for (let i = 0; i < Math.min(rel.size, k); i++) idcg += 1 / Math.log2(i + 2);
  return idcg ? dcg / idcg : 0;
}

export function mrrAt(hits, relevant, k) {
  const rel = new Set(relevant);
  const i = hits.slice(0, k).findIndex((id) => rel.has(id));
  return i < 0 ? 0 : 1 / (i + 1);
}

export function quantile(xs, q) {
  const a = xs.filter((x) => Number.isFinite(x)).sort((x, y) => x - y);
  if (!a.length) return null;
  const pos = (a.length - 1) * q;
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  return a[lo] + (a[hi] - a[lo]) * (pos - lo);
}

export const median = (xs) => quantile(xs, 0.5);

export function summarize(xs) {
  const a = xs.filter((x) => Number.isFinite(x));
  return { n: a.length, median: median(a), p95: quantile(a, 0.95), min: a.length ? Math.min(...a) : null, max: a.length ? Math.max(...a) : null };
}

export function mean(xs) {
  return xs.length ? xs.reduce((s, x) => s + x, 0) / xs.length : 0;
}
