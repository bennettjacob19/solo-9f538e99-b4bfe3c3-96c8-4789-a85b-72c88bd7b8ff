"""方案执行器：按规划串行落盘，真实写出样例产物并强制占用额度。

- 产物内容由「步骤名 + 各依赖文件内容」决定（SHA-256 计数器流），
  因此删除后重建会得到完全一致的字节；
- 分块写入，每写一块前实测目录占用：保留文件 + 未写完的半成品都占额度，
  超限即抛 QuotaExceeded；
- 写入失败立即停下，删除半成品（.part），把失败写进运行元数据；
- 每步之后（以及每块之后）采样目录占用，得到真实峰值。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Dict, List, Tuple

CHUNK = 65536


class QuotaExceeded(Exception):
    """写入过程中实测占用将超过本次上限。"""


def dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except FileNotFoundError:
                pass
    return total


def file_sha256(path: str) -> bytes:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.digest()


def artifact_seed(step_name: str, dep_digests: List[Tuple[str, bytes]]) -> bytes:
    """产物种子 = f(步骤名, 各依赖名及其文件内容摘要)。"""
    h = hashlib.sha256()
    h.update(b"step\x00" + step_name.encode("utf-8"))
    for dep_name, digest in sorted(dep_digests):
        h.update(b"\x00dep\x00" + dep_name.encode("utf-8"))
        h.update(digest)
    return h.digest()


def content_blocks(seed: bytes, size: int):
    """按计数器模式生成确定性的 size 字节流（逐块产出）。"""
    counter = 0
    left = size
    while left > 0:
        block = hashlib.sha256(seed + counter.to_bytes(8, "big")).digest()
        counter += 1
        take = min(left, len(block))
        yield block[:take]
        left -= take


def content_bytes(seed: bytes, size: int) -> bytes:
    return b"".join(content_blocks(seed, size))


def write_artifact(path: str, seed: bytes, size: int, capacity: int,
                   run_dir: str, sample) -> None:
    """分块写出产物；写前做额度检查，失败清除半成品后抛出异常。"""
    tmp = path + ".part"
    written = 0
    buf = b""
    counter = 0
    try:
        with open(tmp, "wb") as fh:
            while written < size:
                if not buf:
                    buf = hashlib.sha256(seed + counter.to_bytes(8, "big")).digest()
                    counter += 1
                take = min(CHUNK, size - written, len(buf))
                piece, buf = buf[:take], buf[take:]
                # 保留文件 + 未写完的半成品都占额度
                if dir_size(run_dir) + len(piece) > capacity:
                    raise QuotaExceeded(
                        f"写入 {os.path.basename(path)} 至 {written} 字节时，"
                        f"再写 {len(piece)} 字节将超过占用上限 {capacity}"
                    )
                fh.write(piece)
                fh.flush()
                written += take
                sample()
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def execute(plan: List[Tuple[str, str]], steps, target: str,
            capacity: int, runs_dir: str, run_id: str = None) -> Dict:
    """串行执行方案，返回运行元数据（同时落盘 <run_id>.json）。"""
    run_id = run_id or uuid.uuid4().hex[:12]
    run_dir = os.path.join(runs_dir, run_id)
    os.makedirs(run_dir, exist_ok=False)
    by_name = {s.name: s for s in steps}
    events: List[Dict] = []
    peak = 0
    built_once = set()
    started = time.time()

    def sample_peak() -> int:
        nonlocal peak
        u = dir_size(run_dir)
        peak = max(peak, u)
        return u

    def apath(name: str) -> str:
        return os.path.join(run_dir, name + ".bin")

    status, error = "success", None
    try:
        for seq, (op, name) in enumerate(plan, 1):
            if op == "delete":
                os.remove(apath(name))
                usage = sample_peak()
                events.append({
                    "seq": seq, "op": "delete", "step": name,
                    "size": 0, "usage": usage,
                    "note": f"回收 {by_name[name].size} 字节",
                })
            else:
                s = by_name[name]
                dep_digests = []
                for d in s.deps:
                    p = apath(d)
                    if not os.path.exists(p):
                        raise RuntimeError(f"依赖 {d} 的产物不在盘上，方案无法执行")
                    dep_digests.append((d, file_sha256(p)))
                rerun = name in built_once
                seed = artifact_seed(name, dep_digests)
                write_artifact(apath(name), seed, s.size, capacity,
                               run_dir, sample_peak)
                built_once.add(name)
                usage = sample_peak()
                events.append({
                    "seq": seq, "op": "rerun" if rerun else "build",
                    "step": name, "size": s.size, "usage": usage,
                    "note": "重建：产物曾被删除，计一次重跑" if rerun else "",
                })
    except (OSError, QuotaExceeded, RuntimeError) as exc:
        status, error = "failed", str(exc)

    meta = {
        "run_id": run_id,
        "status": status,
        "error": error,
        "target": target,
        "capacity": capacity,
        "peak": peak,
        "final_usage": dir_size(run_dir),
        "reruns": sum(1 for e in events if e["op"] == "rerun"),
        "events": events,
        "plan_len": len(plan),
        "target_file": target + ".bin" if status == "success" else None,
        "elapsed_ms": round((time.time() - started) * 1000, 1),
    }
    with open(os.path.join(runs_dir, run_id + ".json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    return meta
