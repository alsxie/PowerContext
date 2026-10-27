"""对基准原始数据做统计检验，输出 bench/analysis.md 与 bench/analysis.json。

作者：FDE WorkMate

只读取 bench/results 与 bench/tuning 中保存的逐查询指标，不重新运行任何推理：
1. 量化质量：同一后端下 FP16 / INT8 / WQ8 相对 FP32 的逐查询 nDCG@10 差值（精确向量检索，
   排除 HNSW 近似误差），报告均值、配对 bootstrap 95% CI（10000 次，固定种子）、
   双侧符号检验 p 值，以及变差 / 持平 / 变好的查询数。
2. 检索模式：混合 vs 仅向量 vs 仅全文（HNSW 实际检索路径），同样做配对检验。
3. RRF 参数：防止“在测试查询上调参再报告同批分数”的乐观偏差，
   对网格结果做 2 折交叉验证（随机划分 200 次）：在一半查询上选最优 (k, window)，
   在另一半上评估，与 SDK 默认 (k=60, window=40) 在同一半上比较。

用法：uv run python scripts/analyze.py
"""
from __future__ import annotations

import glob
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
RNG_SEED = 20261028
N_BOOT = 10000


def load(pattern: str) -> dict:
    files = sorted(glob.glob(str(ROOT / pattern)))
    if not files:
        raise SystemExit(f"缺少原始数据：{pattern}（先运行 node scripts/bench.mjs）")
    return json.loads(Path(files[-1]).read_text(encoding="utf-8"))


def per_query(rows: list[dict], key: str = "ndcg10") -> dict[str, float]:
    return {r["id"]: float(r[key]) for r in rows}


def sign_test(diff: np.ndarray) -> float:
    """双侧符号检验（忽略差值为 0 的查询），精确二项分布。"""
    pos, neg = int((diff > 1e-12).sum()), int((diff < -1e-12).sum())
    n = pos + neg
    if n == 0:
        return 1.0
    k = min(pos, neg)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def paired(a: dict[str, float], b: dict[str, float], rng: np.random.Generator) -> dict:
    """b 相对 a 的逐查询差值统计。"""
    ids = sorted(set(a) & set(b))
    d = np.array([b[i] - a[i] for i in ids])
    boot = d[rng.integers(0, len(d), size=(N_BOOT, len(d)))].mean(axis=1)
    lo, hi = np.percentile(boot, [2.5, 97.5])
    base = np.mean([a[i] for i in ids])
    return {
        "n": len(ids),
        "mean_a": float(base),
        "mean_b": float(np.mean([b[i] for i in ids])),
        "delta": float(d.mean()),
        "delta_pct": float(100 * d.mean() / base) if base else None,
        "ci95": [float(lo), float(hi)],
        "sign_p": sign_test(d),
        "worse": int((d < -1e-12).sum()),
        "same": int((np.abs(d) <= 1e-12).sum()),
        "better": int((d > 1e-12).sum()),
    }


def fmt(x: dict) -> str:
    sig = "显著" if x["ci95"][0] > 0 or x["ci95"][1] < 0 else "不显著"
    return (f"{x['mean_a']:.4f} → {x['mean_b']:.4f} | {x['delta']:+.4f}（{x['delta_pct']:+.1f}%） | "
            f"[{x['ci95'][0]:+.4f}, {x['ci95'][1]:+.4f}] | {x['sign_p']:.3f} | {x['worse']}/{x['same']}/{x['better']} | {sig}")


def rrf_cv(grid: list[dict], rng: np.random.Generator, splits: int = 200) -> dict:
    params = [(g["rankConstant"], g["window"]) for g in grid]
    ids = sorted(per_query(grid[0]["perQuery"]))
    m = np.array([[per_query(g["perQuery"])[i] for i in ids] for g in grid])  # params × queries
    default = params.index((60, 40))
    chosen, gains, tuned_scores, default_scores = {}, [], [], []
    for _ in range(splits):
        perm = rng.permutation(len(ids))
        halves = (perm[: len(ids) // 2], perm[len(ids) // 2:])
        for tr, te in (halves, halves[::-1]):
            best = int(np.argmax(m[:, tr].mean(axis=1)))
            chosen[params[best]] = chosen.get(params[best], 0) + 1
            tuned_scores.append(m[best, te].mean())
            default_scores.append(m[default, te].mean())
            gains.append(m[best, te].mean() - m[default, te].mean())
    g = np.array(gains)
    full_best = int(np.argmax(m.mean(axis=1)))
    return {
        "params_grid": params,
        "in_sample_best": {"param": params[full_best], "ndcg10": float(m[full_best].mean())},
        "sdk_default": {"param": (60, 40), "ndcg10": float(m[default].mean())},
        "cv_tuned_ndcg10": float(np.mean(tuned_scores)),
        "cv_default_ndcg10": float(np.mean(default_scores)),
        "cv_gain_mean": float(g.mean()),
        "cv_gain_p2_5_p97_5": [float(np.percentile(g, 2.5)), float(np.percentile(g, 97.5))],
        "cv_gain_positive_ratio": float((g > 0).mean()),
        "chosen_freq": {f"k={k},w={w}": c for (k, w), c in sorted(chosen.items(), key=lambda x: -x[1])},
        "selected_param_ndcg10": {f"k={p[0]},w={p[1]}": float(m[i].mean()) for i, p in enumerate(params) if p in [(10, 20), (60, 40), params[full_best]]},
    }


def main() -> int:
    rng = np.random.default_rng(RNG_SEED)
    out: dict = {"author": "FDE WorkMate", "seed": RNG_SEED, "bootstrap": N_BOOT}
    lines = ["# 统计检验（自动生成）", "", "作者：FDE WorkMate", "",
             f"方法：逐查询配对差值；95% CI 为配对 bootstrap（{N_BOOT} 次，种子 {RNG_SEED}）；p 为双侧精确符号检验；"
             "“变差/持平/变好”为查询数。CI 不跨 0 记为显著。", ""]

    lines += ["## 1. 量化对检索质量的影响（精确向量检索 nDCG@10，相对同后端 FP32）", "",
              "| 后端 | 精度 | FP32 → 量化 | 差值 | 95% CI | 符号检验 p | 变差/持平/变好 | 结论 |", "|---|---|---|---|---|---|---|---|"]
    out["quantization"] = {}
    for backend in ("webgpu", "wasm"):
        base = per_query(load(f"bench/results/*quality-fp32-{backend}-cold.json")["quality"]["exact"]["perQuery"])
        for dtype in ("fp16", "wq8", "int8"):
            q = per_query(load(f"bench/results/*quality-{dtype}-{backend}-cold.json")["quality"]["exact"]["perQuery"])
            x = paired(base, q, rng)
            out["quantization"][f"{dtype}-{backend}"] = x
            lines.append(f"| {backend} | {dtype} | {fmt(x)} |")
    # Recall@10 是否变化
    lines += ["", "同一组运行的精确检索 Recall@10："]
    for backend in ("webgpu", "wasm"):
        vals = []
        for dtype in ("fp32", "fp16", "wq8", "int8"):
            rows = load(f"bench/results/*quality-{dtype}-{backend}-cold.json")["quality"]["exact"]["perQuery"]
            vals.append(f"{dtype} {np.mean([r['recall10'] for r in rows]):.3f}")
        lines.append(f"- {backend}：" + "，".join(vals))

    lines += ["", "## 2. 检索模式对比（FP32 + WASM，seekdb HNSW 实际检索路径，nDCG@10）", "",
              "| 对比 | A → B | 差值 | 95% CI | 符号检验 p | 变差/持平/变好 | 结论 |", "|---|---|---|---|---|---|---|"]
    r = load("bench/results/*quality-fp32-wasm-cold.json")["quality"]
    modes = {k: per_query(r[k]["perQuery"]) for k in ("fulltext", "vector", "hybrid", "exact")}
    out["modes"] = {}
    for a, b in (("vector", "hybrid"), ("fulltext", "hybrid"), ("exact", "vector")):
        x = paired(modes[a], modes[b], rng)
        out["modes"][f"{a}->{b}"] = x
        lines.append(f"| {a} → {b} | {fmt(x)} |")
    rec = {k: per_query(r[k]["perQuery"], "recall10") for k in ("vector", "hybrid")}
    x = paired(rec["vector"], rec["hybrid"], rng)
    out["modes"]["vector->hybrid recall10"] = x
    lines.append(f"| vector → hybrid（Recall@10） | {fmt(x)} |")

    lines += ["", "## 3. RRF 参数：2 折交叉验证（随机划分 200 次）", "",
              "| 网格数据 | 全量最优（样本内） | SDK 默认 k=60,w=40 | 交叉验证：调参后 | 交叉验证：默认 | 增益均值 [2.5%, 97.5%] | 增益>0 比例 | 被选中最多的参数 |",
              "|---|---|---|---|---|---|---|---|"]
    out["rrf_cv"] = {}
    for name in ("fp16-webgpu", "int8-wasm"):
        g = load(f"bench/tuning/*grid-ik-{name}.json")["hybridGrid"]
        x = rrf_cv(g, rng)
        out["rrf_cv"][name] = x
        top = list(x["chosen_freq"].items())[:3]
        lines.append(f"| {name} | k={x['in_sample_best']['param'][0]},w={x['in_sample_best']['param'][1]}：{x['in_sample_best']['ndcg10']:.4f} | "
                     f"{x['sdk_default']['ndcg10']:.4f} | {x['cv_tuned_ndcg10']:.4f} | {x['cv_default_ndcg10']:.4f} | "
                     f"{x['cv_gain_mean']:+.4f} [{x['cv_gain_p2_5_p97_5'][0]:+.4f}, {x['cv_gain_p2_5_p97_5'][1]:+.4f}] | "
                     f"{100 * x['cv_gain_positive_ratio']:.0f}% | {'；'.join(f'{k}（{v}次）' for k, v in top)} |")
    lines += ["", "交叉验证的“调参后”分数来自未参与选参的那一半查询，是对线上效果的无偏估计；"
              "它与样本内最优之间的差距即调参的乐观偏差。"]

    (ROOT / "bench" / "analysis.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "bench" / "analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
