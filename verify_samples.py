"""内置样例试跑与核对脚本。

覆盖：
1. 样例A「保留产物」：0 删除 0 重跑，峰值应等于全部产物之和；
2. 样例B「删后重建」：恰好 1 次重跑，峰值应顶到容量上限；
   并逐字节核对重建产物与下游产物内容（内容由步骤名+依赖内容决定）；
3. 无解情形：重跑上限 0 的样例B、容量永远不够的情形 —— 判无解且目录零产物；
4. 写失败路径：伪造超限方案，执行器应停下、清除半成品并报告失败；
5. Web 冒烟：GET /、POST /run 两个样例、下载目标文件、无解页面。

用法：python3 verify_samples.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile

from executor import (artifact_seed, content_bytes, dir_size, execute,
                      file_sha256)
from planner import PlanError, Step, solve
from samples import SAMPLES

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  | {detail}" if detail else ""))


def to_steps(sample):
    return [
        Step(s["name"], s["size"],
             [d.strip() for d in s["deps"].split(",") if d.strip()])
        for s in sample["steps"]
    ]


def sha(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def sample_a(tmp):
    print("\n== 样例A：保留产物 ==")
    s = SAMPLES["a"]
    steps = to_steps(s)
    plan, info = solve(steps, s["target"], s["capacity"], s["rerun_limit"])
    meta = execute(plan, steps, s["target"], s["capacity"], tmp)
    run_dir = os.path.join(tmp, meta["run_id"])
    total = sum(x.size for x in steps)

    check("A 求解成功", True, f"方案 {len(plan)} 步")
    check("A 零删除", info["deletes"] == 0, f"deletes={info['deletes']}")
    check("A 零重跑", info["reruns"] == 0, f"reruns={info['reruns']}")
    check("A 执行成功", meta["status"] == "success", meta.get("error") or "")
    check("A 峰值 == 全部产物之和", meta["peak"] == total,
          f"peak={meta['peak']} total={total}")
    check("A 峰值 <= 容量上限", meta["peak"] <= s["capacity"])
    check("A 最终落盘 == 全部产物之和", meta["final_usage"] == total,
          f"final={meta['final_usage']}")
    check("A 实测目录占用与记录一致", dir_size(run_dir) == meta["final_usage"])
    tgt = os.path.join(run_dir, "link_firmware.bin")
    check("A 目标产物落盘且字节数正确",
          os.path.exists(tgt) and os.path.getsize(tgt) == 350000)
    # 内容核对：link_firmware = f(步骤名, compile_boot 与 compile_kernel 的内容)
    fetch = content_bytes(artifact_seed("fetch_sources", []), 200000)
    tool = content_bytes(artifact_seed("toolchain", []), 300000)
    boot = content_bytes(
        artifact_seed("compile_boot", [("fetch_sources", sha(fetch))]), 250000)
    kernel = content_bytes(artifact_seed("compile_kernel", [
        ("fetch_sources", sha(fetch)), ("toolchain", sha(tool))]), 400000)
    expect = content_bytes(artifact_seed("link_firmware", [
        ("compile_boot", sha(boot)), ("compile_kernel", sha(kernel))]), 350000)
    check("A 目标产物内容与规则推导一致", file_sha256(tgt) == sha(expect))
    return meta


def sample_b(tmp):
    print("\n== 样例B：删后重建 ==")
    s = SAMPLES["b"]
    steps = to_steps(s)
    plan, info = solve(steps, s["target"], s["capacity"], s["rerun_limit"])
    for i, (op, name) in enumerate(plan, 1):
        print(f"    方案{i}: {op:6s} {name}")
    meta = execute(plan, steps, s["target"], s["capacity"], tmp)
    run_dir = os.path.join(tmp, meta["run_id"])

    check("B 求解成功", True, f"方案 {len(plan)} 步")
    check("B 恰好 1 次重跑", info["reruns"] == 1, f"reruns={info['reruns']}")
    check("B 发生删除回收", info["deletes"] >= 1, f"deletes={info['deletes']}")
    check("B 执行成功", meta["status"] == "success", meta.get("error") or "")
    check("B 重跑记录与规划一致", meta["reruns"] == info["reruns"],
          f"meta={meta['reruns']} plan={info['reruns']}")
    check("B 峰值 == 容量上限 800000", meta["peak"] == 800000,
          f"peak={meta['peak']}")
    check("B 峰值 <= 容量上限", meta["peak"] <= s["capacity"])
    check("B 实测目录占用与记录一致", dir_size(run_dir) == meta["final_usage"],
          f"final={meta['final_usage']}")
    tgt = os.path.join(run_dir, "seal_device.bin")
    check("B 目标产物落盘且字节数正确",
          os.path.exists(tgt) and os.path.getsize(tgt) == 100000)

    # 关键核对：extract_keys 被删后重建，内容须与首次逐字节一致；
    # 下游 pack_image / seal_device 的内容须与「由当前依赖内容推导」一致。
    x_expect = content_bytes(artifact_seed("extract_keys", []), 300000)
    x_disk = os.path.join(run_dir, "extract_keys.bin")
    check("B 重建的 extract_keys 与确定性推导一致",
          os.path.exists(x_disk) and file_sha256(x_disk) == sha(x_expect))
    y_expect = content_bytes(
        artifact_seed("sign_bootloader", [("extract_keys", sha(x_expect))]), 200000)
    z_expect = content_bytes(
        artifact_seed("pack_image", [("sign_bootloader", sha(y_expect))]), 400000)
    z_disk = os.path.join(run_dir, "pack_image.bin")
    check("B pack_image 内容与依赖链推导一致",
          os.path.exists(z_disk) and file_sha256(z_disk) == sha(z_expect))
    t_expect = content_bytes(artifact_seed("seal_device", [
        ("extract_keys", sha(x_expect)), ("pack_image", sha(z_expect))]), 100000)
    check("B 目标 seal_device 内容与依赖链推导一致",
          file_sha256(tgt) == sha(t_expect))
    return meta


def unsolvable_cases(tmp):
    print("\n== 无解情形 ==")
    s = SAMPLES["b"]
    steps = to_steps(s)
    before = set(os.listdir(tmp))
    try:
        solve(steps, s["target"], s["capacity"], 0)
        check("样例B+重跑上限0 判无解", False, "居然给出了方案")
    except PlanError as e:
        check("样例B+重跑上限0 判无解", True, str(e))
    try:
        solve(steps, s["target"], 500000, 99)
        check("容量装不下依赖闭包 判无解", False, "居然给出了方案")
    except PlanError as e:
        check("容量装不下依赖闭包 判无解", True, str(e))
    try:
        solve(steps, "ghost_step", 10 ** 9, 99)
        check("目标不存在 判无解", False)
    except PlanError as e:
        check("目标不存在 判无解", True, str(e))
    cyclic = [Step("p", 10, ["q"]), Step("q", 10, ["p"])]
    try:
        solve(cyclic, "p", 10 ** 6, 0)
        check("依赖成环 判无解", False)
    except PlanError as e:
        check("依赖成环 判无解", True, str(e))
    after = set(os.listdir(tmp))
    check("无解时未写入任何产物", before == after,
          f"新增文件: {after - before or '无'}")


def write_failure_path(tmp):
    print("\n== 写失败路径（执行器硬约束） ==")
    steps = [Step("x", 100, []), Step("y", 100, [])]
    # 伪造一个规划器绝不会给的方案：容量 150 却连着写两个 100
    meta = execute([("build", "x"), ("build", "y")], steps, "y", 150, tmp)
    run_dir = os.path.join(tmp, meta["run_id"])
    leftovers = os.listdir(run_dir)
    check("超限写入被叫停", meta["status"] == "failed", meta.get("error") or "")
    check("失败原因是占用上限", "上限" in (meta.get("error") or ""),
          meta.get("error") or "")
    check("半成品已清除（无 .part，无 y.bin）",
          "y.bin" not in leftovers
          and not any(f.endswith(".part") for f in leftovers),
          f"目录剩余: {leftovers}")
    check("已完成的 x.bin 保留", "x.bin" in leftovers)
    check("事件停在被叫停处", len(meta["events"]) == 1,
          f"events={len(meta['events'])}")


def web_smoke(tmp):
    print("\n== Web 冒烟 ==")
    os.environ["RUNS_DIR"] = tmp
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import importlib
    import app as webapp
    importlib.reload(webapp)  # 让 RUNS_DIR 生效
    webapp.app.config["TESTING"] = True
    client = webapp.app.test_client()

    r = client.get("/")
    check("GET / 返回 200", r.status_code == 200)
    check("首页含样例入口", "样例A" in r.get_data(as_text=True)
          and "样例B" in r.get_data(as_text=True))

    def post_sample(key):
        s = SAMPLES[key]
        data = {"target": s["target"], "capacity": str(s["capacity"]),
                "rerun_limit": str(s["rerun_limit"])}
        for i in range(10):
            if i < len(s["steps"]):
                st = s["steps"][i]
                data[f"name_{i}"] = st["name"]
                data[f"deps_{i}"] = st["deps"]
                data[f"size_{i}"] = str(st["size"])
            else:
                data[f"name_{i}"] = data[f"deps_{i}"] = data[f"size_{i}"] = ""
        return client.post("/run", data=data)

    r = post_sample("a")
    body = r.get_data(as_text=True)
    check("POST 样例A 执行成功", r.status_code == 200 and "执行成功" in body)
    check("样例A 页面显示峰值 1500000", "1500000" in body)

    r = post_sample("b")
    body = r.get_data(as_text=True)
    check("POST 样例B 执行成功", r.status_code == 200 and "执行成功" in body)
    check("样例B 页面显示 1 次重跑", "重跑 <b>1</b>" in body)
    check("样例B 页面显示峰值 800000", "800000" in body)

    # 从页面里找到下载链接并真正下载目标产物
    import re
    m = re.search(r'href="(/download/[0-9a-f]{12})"', body)
    check("成功页给出下载链接", m is not None)
    if m:
        dl = client.get(m.group(1))
        check("下载目标产物 200 且 100000 字节",
              dl.status_code == 200 and len(dl.get_data()) == 100000,
              f"status={dl.status_code} bytes={len(dl.get_data())}")

    # 无解：样例B 但重跑上限 0
    s = SAMPLES["b"]
    data = {"target": s["target"], "capacity": str(s["capacity"]),
            "rerun_limit": "0"}
    for i, st in enumerate(s["steps"]):
        data[f"name_{i}"] = st["name"]
        data[f"deps_{i}"] = st["deps"]
        data[f"size_{i}"] = str(st["size"])
    r = client.post("/run", data=data)
    check("无解页提示且不写产物",
          r.status_code == 200 and "无解" in r.get_data(as_text=True))


def main():
    tmp = tempfile.mkdtemp(prefix="ci-drill-verify-")
    print(f"验证工作目录: {tmp}")
    try:
        sample_a(tmp)
        sample_b(tmp)
        unsolvable_cases(tmp)
        write_failure_path(tmp)
        try:
            web_smoke(tmp)
        except Exception as exc:  # Flask 缺失等环境问题单独报告
            check("Web 冒烟可运行", False, repr(exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok in CHECKS if not ok]
    print(f"\n==== {len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过"
          + (f"，未过: {failed}" if failed else "，全部通过") + " ====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
