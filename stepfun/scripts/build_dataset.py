"""构建可复现的中文检索评测集（MMarco-zh mini）。

作者：FDE WorkMate

来源：C-MTEB/MMarcoRetrieval（corpus + queries）与 C-MTEB/MMarcoRetrieval-qrels（dev），
即 mMARCO 中文版（MS MARCO 段落检索的机器翻译版本，仅用于研究评测）。

构建方式（固定随机种子，结果写入 data/mmarco-zh-mini/ 并随仓库提交）：
1. 在 dev qrels 中随机抽取 N_QUERIES 条查询，取其全部相关段落为正例；
2. 难负例：在 10 万段落上用字符 bigram BM25 为每条查询取前 HARD_NEG 个非正例段落，
   保证评测集对“全文”和“向量”两路都有区分度，不会因随机负例过于简单而全部满分；
3. 随机负例补足到 N_DOCS 条；
4. 同时按导入批次切分（batch-1 / batch-2），用于演示和验证增量写入：
   第二批包含部分查询的正例，导入并刷新后这些查询才应被召回。

用法：uv run python scripts/build_dataset.py
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "mmarco-zh-mini"
CACHE = ROOT / ".cache" / "datasets"
ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com").rstrip("/")

SOURCES = {
    "corpus": ("C-MTEB/MMarcoRetrieval", "539bbde593d947e2a124ba72651aafc09eb33fc2",
               "data/corpus-00000-of-00001-b8cdf7f89c113423.parquet"),
    "queries": ("C-MTEB/MMarcoRetrieval", "539bbde593d947e2a124ba72651aafc09eb33fc2",
                "data/queries-00000-of-00001-4542b172a462173e.parquet"),
    "qrels": ("C-MTEB/MMarcoRetrieval-qrels", "bae08bb7bddbedb96c7e7db52018a55167b67f89",
              "data/dev-00000-of-00001-6c20deed08d304e4.parquet"),
}
SEED = 20261028
N_QUERIES = 100
N_DOCS = 2000
HARD_NEG = 8
SECOND_BATCH_QUERY_RATIO = 0.3

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler(urllib.request.getproxies_environment()))
OPENER.addheaders = [("User-Agent", "stepfun-edge-kb/1.0 (+build_dataset.py)")]


def fetch(key: str) -> Path:
    repo, rev, rel = SOURCES[key]
    dst = CACHE / repo.replace("/", "__") / rev / rel
    if dst.exists():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    url = f"{ENDPOINT}/datasets/{repo}/resolve/{rev}/{rel}"
    print(f"[get] {url}")
    tmp = dst.with_suffix(".part")
    with OPENER.open(url, timeout=60) as r, tmp.open("wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    tmp.replace(dst)
    return dst


def bigrams(text: str) -> list[str]:
    s = "".join(ch for ch in text.lower() if not ch.isspace())
    return [s[i:i + 2] for i in range(len(s) - 1)]


class BM25:
    def __init__(self, docs: dict[str, str], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf: dict[str, Counter] = {}
        self.df: Counter = Counter()
        self.postings: dict[str, list[str]] = defaultdict(list)
        for pid, text in docs.items():
            c = Counter(bigrams(text))
            self.tf[pid] = c
            for t in c:
                self.df[t] += 1
                self.postings[t].append(pid)
        self.n = len(docs)
        self.avg = sum(sum(c.values()) for c in self.tf.values()) / max(self.n, 1)

    def top(self, query: str, k: int, exclude: set[str]) -> list[str]:
        scores: dict[str, float] = defaultdict(float)
        for t in set(bigrams(query)):
            df = self.df.get(t)
            if not df:
                continue
            idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
            for pid in self.postings[t]:
                tf = self.tf[pid][t]
                dl = sum(self.tf[pid].values())
                scores[pid] += idf * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * dl / self.avg))
        ranked = sorted((p for p in scores if p not in exclude), key=lambda p: -scores[p])
        return ranked[:k]


def main() -> int:
    import pyarrow.parquet as pq

    corpus = {r["id"]: r["text"] for r in pq.read_table(fetch("corpus")).to_pylist()}
    queries = {r["id"]: r["text"] for r in pq.read_table(fetch("queries")).to_pylist()}
    qrels: dict[str, set[str]] = defaultdict(set)
    for r in pq.read_table(fetch("qrels")).to_pylist():
        if r["score"] > 0 and r["pid"] in corpus:
            qrels[r["qid"]].add(r["pid"])

    rng = random.Random(SEED)
    candidates = sorted(q for q in qrels if q in queries)
    picked = sorted(rng.sample(candidates, N_QUERIES), key=int)
    positives = set().union(*(qrels[q] for q in picked))
    print(f"queries={len(picked)} positives={len(positives)} corpus={len(corpus)}")

    bm25 = BM25(corpus)
    hard: set[str] = set()
    for q in picked:
        hard.update(bm25.top(queries[q], HARD_NEG, exclude=qrels[q]))
    hard -= positives
    pool = sorted(set(corpus) - positives - hard, key=int)
    need = N_DOCS - len(positives) - len(hard)
    rand_neg = set(rng.sample(pool, max(need, 0)))
    doc_ids = sorted(positives | hard | rand_neg, key=int)
    print(f"hard_neg={len(hard)} random_neg={len(rand_neg)} docs={len(doc_ids)}")

    # 增量导入切分：部分查询的正例只出现在第二批
    late_queries = set(rng.sample(picked, int(N_QUERIES * SECOND_BATCH_QUERY_RATIO)))
    late_pos = set().union(*(qrels[q] for q in late_queries)) - set().union(
        *(qrels[q] for q in picked if q not in late_queries))
    others = [d for d in doc_ids if d not in late_pos]
    rng.shuffle(others)
    batch2 = set(late_pos) | set(others[: N_DOCS // 4 - len(late_pos)])

    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "corpus.jsonl").open("w", encoding="utf-8") as f:
        for d in doc_ids:
            f.write(json.dumps({"id": f"p{d}", "text": corpus[d],
                                "batch": 2 if d in batch2 else 1}, ensure_ascii=False) + "\n")
    with (OUT / "queries.jsonl").open("w", encoding="utf-8") as f:
        for q in picked:
            f.write(json.dumps({"id": f"q{q}", "text": queries[q],
                                "relevant": sorted(f"p{p}" for p in qrels[q]),
                                "needs_batch": 2 if q in late_queries else 1}, ensure_ascii=False) + "\n")
    lengths = sorted(len(corpus[d]) for d in doc_ids)
    meta = {
        "author": "FDE WorkMate",
        "name": "mmarco-zh-mini",
        "sources": {k: {"repo": v[0], "revision": v[1], "file": v[2]} for k, v in SOURCES.items()},
        "split": "dev",
        "seed": SEED,
        "n_queries": len(picked),
        "n_docs": len(doc_ids),
        "n_positive": len(positives),
        "n_hard_negative": len(hard),
        "hard_negative_method": f"char-bigram BM25 top-{HARD_NEG} per query (excluding positives)",
        "batch_sizes": {"1": len(doc_ids) - len(batch2), "2": len(batch2)},
        "queries_needing_batch2": len(late_queries),
        "doc_chars": {"median": lengths[len(lengths) // 2], "max": lengths[-1], "total": sum(lengths)},
    }
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
