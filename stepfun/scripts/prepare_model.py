"""获取 embedding 模型并生成 INT8 / FP16 量化版本。

作者：FDE WorkMate

流程：
1. 从 Hugging Face（默认国内镜像 hf-mirror.com）按固定 revision 下载
   Xenova/bge-small-zh-v1.5 的 FP32 ONNX 与 tokenizer（权重来源 BAAI/bge-small-zh-v1.5，MIT）。
2. 本地量化：
   - INT8：onnxruntime.quantization.quantize_dynamic，权重（含词向量 Gather）per-channel 对称 QInt8，
     激活在运行时动态量化（MatMul/Gemm → MatMulInteger / DynamicQuantizeLinear）。
   - WQ8（INT8 权重量化，面向 WebGPU）：MatMul → MatMulNBits(bits=8, block=32, 对称)，
     词向量 Gather 用 QInt8 + DequantizeLinear。只量化权重、激活保持浮点，避免动态量化算子
     （DynamicQuantizeLinear / MatMulInteger）在 WebGPU 上没有 kernel 而回退 CPU。
   - FP16：onnxruntime.transformers.float16，权重与中间计算转 FP16，输入输出保持 FP32/INT64。
3. 用 onnxruntime CPU 对比量化前后的句向量余弦相似度做离线健全性检查（检索质量在浏览器里另测）。
4. 产出 models/manifest.json：文件大小、sha256、压缩比、来源与 revision。

用法：uv run python scripts/prepare_model.py [--force]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"

REPO = "Xenova/bge-small-zh-v1.5"
UPSTREAM = "BAAI/bge-small-zh-v1.5"
# 固定 revision，保证可复现；可用环境变量覆盖
REVISION = os.environ.get("MODEL_REVISION", "75c43b069aac4d136ba6bc1122f995fedcfd2781")
ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com").rstrip("/")
# 只使用环境变量里的代理（不读取 macOS 系统代理），国内镜像直连更稳定
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler(urllib.request.getproxies_environment()))
OPENER.addheaders = [("User-Agent", "stepfun-edge-kb/1.0 (+prepare_model.py)")]

FILES = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "onnx/model.onnx",
]

SANITY_TEXTS = [
    "seekdb 是一款 AI 原生的混合检索数据库",
    "浏览器端使用 WebGPU 执行 embedding 推理",
    "今天的天气适合去公园散步",
    "如何在笔记本电脑上安装 Python 开发环境",
]


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_revision() -> str:
    """把分支名解析为 commit sha，写入 manifest 便于复现。"""
    if len(REVISION) == 40:
        return REVISION
    url = f"{ENDPOINT}/api/models/{REPO}/revision/{REVISION}"
    with OPENER.open(url, timeout=30) as r:
        return json.load(r)["sha"]


def download(rev: str, target: Path, force: bool) -> None:
    for rel in FILES:
        dst = target / rel
        if dst.exists() and not force:
            print(f"[skip] {rel}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        url = f"{ENDPOINT}/{REPO}/resolve/{rev}/{rel}"
        print(f"[get ] {url}")
        tmp = dst.with_suffix(dst.suffix + ".part")
        fetch_resumable(url, tmp)
        tmp.replace(dst)


def fetch_resumable(url: str, tmp: Path, attempts: int = 8) -> None:
    """镜像站大文件偶发断流且不报错：按 Content-Length 校验，断点续传重试。"""
    total = None
    for i in range(attempts):
        have = tmp.stat().st_size if tmp.exists() else 0
        if total is not None and have >= total:
            break
        req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        try:
            with OPENER.open(req, timeout=30) as r:
                if r.status == 206:
                    total = int(r.headers["Content-Range"].rsplit("/", 1)[1])
                    mode = "ab"
                else:
                    total = int(r.headers.get("Content-Length") or 0) or None
                    mode = "wb"
                with tmp.open(mode) as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
        except (OSError, ValueError) as e:  # 网络中断，进入下一轮续传
            print(f"[retry] {i + 1}/{attempts}: {e}")
        size = tmp.stat().st_size
        if total is None or size >= total:
            return
        print(f"[retry] {i + 1}/{attempts}: 已下载 {size}/{total}")
    if total is not None and tmp.stat().st_size != total:
        raise RuntimeError(f"下载不完整：{url}")


def quantize_int8(src: Path, dst: Path) -> None:
    from onnxruntime.quantization import QuantType, quantize_dynamic

    quantize_dynamic(
        model_input=str(src),
        model_output=str(dst),
        weight_type=QuantType.QInt8,
        per_channel=True,
        reduce_range=False,
        # Gather 覆盖词向量表（21128x512，占 FP32 模型近一半体积）
        op_types_to_quantize=["MatMul", "Gemm", "Gather"],
    )


def quantize_wq8(src: Path, dst: Path) -> None:
    import tempfile

    import onnx
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from onnxruntime.quantization.matmul_nbits_quantizer import (
        DefaultWeightOnlyQuantConfig,
        MatMulNBitsQuantizer,
    )

    with tempfile.TemporaryDirectory() as tmp:
        # 先量化词向量（Gather），再把 MatMul 换成 MatMulNBits；顺序反过来时 onnx 形状推断会失败
        g8 = Path(tmp) / "gather_int8.onnx"
        quantize_dynamic(str(src), str(g8), weight_type=QuantType.QInt8, per_channel=False, op_types_to_quantize=["Gather"])
        q = MatMulNBitsQuantizer(
            onnx.load(str(g8)),
            algo_config=DefaultWeightOnlyQuantConfig(block_size=32, is_symmetric=True, bits=8, op_types_to_quantize=("MatMul",)),
        )
        q.process()
        q.model.save_model_to_file(str(dst), False)


def convert_fp16(src: Path, dst: Path) -> None:
    import onnx
    # onnxruntime 自带的 float16 转换器会正确改写 Cast(to=FLOAT) 节点；
    # onnxconverter_common 的同名函数在本模型上会留下 Cast 输出类型不一致的问题
    from onnxruntime.transformers.float16 import convert_float_to_float16

    model = onnx.load(str(src))
    # keep_io_types：输入 input_ids 等保持原类型，输出保持 FP32，便于前端池化/归一化
    model16 = convert_float_to_float16(model, keep_io_types=True)
    onnx.save(model16, str(dst))


def embed(model_path: Path, tokenizer_path: Path, texts: list[str]):
    """离线健全性检查：CLS 池化 + L2 归一化（与 bge 官方一致）。"""
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer  # onnxruntime 不带分词器，下面做降级处理

    tok = Tokenizer.from_file(str(tokenizer_path))
    enc = tok.encode_batch(texts)
    n = max(len(e.ids) for e in enc)
    ids = np.zeros((len(texts), n), dtype=np.int64)
    mask = np.zeros_like(ids)
    for i, e in enumerate(enc):
        ids[i, : len(e.ids)] = e.ids
        mask[i, : len(e.ids)] = 1
    sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    feeds = {"input_ids": ids, "attention_mask": mask}
    if "token_type_ids" in {i.name for i in sess.get_inputs()}:
        feeds["token_type_ids"] = np.zeros_like(ids)
    out = sess.run(None, feeds)[0][:, 0, :].astype("float32")
    return out / np.linalg.norm(out, axis=1, keepdims=True)


def sanity(target: Path, variants: dict[str, Path]) -> dict:
    try:
        import tokenizers  # noqa: F401
    except ImportError:
        print("[warn] 未安装 tokenizers，跳过离线一致性检查")
        return {}
    import numpy as np

    base = embed(variants["fp32"], target / "tokenizer.json", SANITY_TEXTS)
    report = {}
    for name, path in variants.items():
        if name == "fp32":
            continue
        v = embed(path, target / "tokenizer.json", SANITY_TEXTS)
        cos = (base * v).sum(axis=1)
        report[name] = {"min_cosine_vs_fp32": float(cos.min()), "mean_cosine_vs_fp32": float(np.mean(cos))}
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="重新下载并重新量化")
    args = ap.parse_args()

    rev = resolve_revision()
    target = MODELS / REPO
    print(f"model {REPO} @ {rev} -> {target}")
    download(rev, target, args.force)

    onnx_dir = target / "onnx"
    fp32 = onnx_dir / "model.onnx"
    # 文件名遵循 Transformers.js 的 dtype 约定：fp16 -> model_fp16.onnx，int8 -> model_int8.onnx
    int8 = onnx_dir / "model_int8.onnx"
    fp16 = onnx_dir / "model_fp16.onnx"
    wq8 = onnx_dir / "model_wq8.onnx"
    timings = {}
    for name, dst, fn in (("int8", int8, quantize_int8), ("fp16", fp16, convert_fp16), ("wq8", wq8, quantize_wq8)):
        if dst.exists() and not args.force:
            print(f"[skip] {dst.name}")
            continue
        t = time.perf_counter()
        fn(fp32, dst)
        timings[name] = round(time.perf_counter() - t, 2)
        print(f"[quant] {name} -> {dst.name} ({timings[name]}s)")

    # 跳过已存在的量化产物时保留上次记录的量化耗时
    old = MODELS / "manifest.json"
    if old.exists():
        timings = {**json.loads(old.read_text(encoding="utf-8")).get("quantize_seconds", {}), **timings}
    variants = {"fp32": fp32, "fp16": fp16, "int8": int8, "wq8": wq8}
    base_size = fp32.stat().st_size
    # 同一口径：浏览器加载一个精度变体时实际下载的全部文件 = 该 onnx + 共享的 tokenizer/config
    shared = {rel: (target / rel).stat().st_size for rel in FILES if not rel.endswith(".onnx")}
    shared_bytes = sum(shared.values())
    manifest = {
        "author": "FDE WorkMate",
        "model_id": REPO,
        "upstream": UPSTREAM,
        "license": "MIT",
        "revision": rev,
        "endpoint": ENDPOINT,
        "pooling": "cls",
        "normalize": True,
        "dimension": 512,
        "variants": {},
        "quantization": {
            "int8": "onnxruntime.quantization.quantize_dynamic(weight_type=QInt8, per_channel=True, op_types=[MatMul, Gemm, Gather])",
            "fp16": "onnxruntime.transformers.float16.convert_float_to_float16(keep_io_types=True)",
            "wq8": "Gather: quantize_dynamic(QInt8) + MatMul: MatMulNBitsQuantizer(bits=8, block_size=32, symmetric)",
        },
        "quantize_seconds": timings,
        "shared_files": shared,
        "shared_bytes": shared_bytes,
    }
    for name, path in variants.items():
        size = path.stat().st_size
        manifest["variants"][name] = {
            "file": str(path.relative_to(target)),
            "bytes": size,
            "mb": round(size / 1024 / 1024, 2),
            "compression_ratio": round(base_size / size, 3),
            "total_bytes": size + shared_bytes,
            "total_mb": round((size + shared_bytes) / 1024 / 1024, 2),
            "total_compression_ratio": round((base_size + shared_bytes) / (size + shared_bytes), 3),
            "sha256": sha256(path),
        }
    manifest["offline_sanity"] = sanity(target, variants)
    out = MODELS / "manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest["variants"], indent=2))
    print(json.dumps(manifest["offline_sanity"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
