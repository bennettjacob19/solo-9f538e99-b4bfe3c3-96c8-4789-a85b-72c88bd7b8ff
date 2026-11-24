"""固件 CI 工作盘演练页。

一个页面完成：填步骤（<=10）与产物字节数、选目标、设占用上限与重跑上限；
有解则按方案串行真实落盘，无解则提示且不写产物；页面列出实际执行/删除/
重跑顺序与占用峰值，成功后可下载目标产物。
"""
from __future__ import annotations

import json
import os
import re

from flask import Flask, abort, render_template, request, send_file

from executor import execute
from planner import MAX_STEPS, PlanError, Step, solve
from samples import SAMPLES

app = Flask(__name__)
RUNS_DIR = os.environ.get("RUNS_DIR", os.path.join(app.root_path, "runs"))
os.makedirs(RUNS_DIR, exist_ok=True)

MAX_SIZE = 64 * 1024 * 1024          # 单步产物上限，防误填
MAX_CAPACITY = 1 << 30               # 占用上限的表单上限
NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
RUN_ID_RE = re.compile(r"[0-9a-f]{12}")


def _empty_rows():
    return [{"name": "", "deps": "", "size": ""} for _ in range(MAX_STEPS)]


def _rows_from_sample(sample):
    rows = _empty_rows()
    for i, s in enumerate(sample["steps"]):
        rows[i] = {"name": s["name"], "deps": s["deps"], "size": str(s["size"])}
    return rows


def _rows_from_form(form):
    rows = _empty_rows()
    for i in range(MAX_STEPS):
        rows[i] = {
            "name": form.get(f"name_{i}", "").strip(),
            "deps": form.get(f"deps_{i}", "").strip(),
            "size": form.get(f"size_{i}", "").strip(),
        }
    return rows


def _render(rows, target, capacity, rerun_limit, result=None):
    return render_template(
        "index.html",
        samples=SAMPLES,
        rows=rows,
        target=target,
        capacity=capacity,
        rerun_limit=rerun_limit,
        result=result,
        max_size=MAX_SIZE,
        max_capacity=MAX_CAPACITY,
    )


@app.get("/")
def index():
    sample = SAMPLES.get(request.args.get("sample", ""))
    if sample:
        return _render(_rows_from_sample(sample), sample["target"],
                       sample["capacity"], sample["rerun_limit"])
    return _render(_empty_rows(), "", "", "")


@app.post("/run")
def run():
    form = request.form
    rows = _rows_from_form(form)
    target = form.get("target", "").strip()
    capacity_s = form.get("capacity", "").strip()
    rerun_s = form.get("rerun_limit", "").strip()

    errors = []
    steps = []
    for i, row in enumerate(rows):
        name, deps_s, size_s = row["name"], row["deps"], row["size"]
        if not name and not deps_s and not size_s:
            continue
        if not NAME_RE.fullmatch(name):
            errors.append(f"第 {i + 1} 行步骤名「{name}」非法：仅限 1-32 位字母/数字/_/-")
            continue
        try:
            size = int(size_s)
            if not 1 <= size <= MAX_SIZE:
                raise ValueError
        except ValueError:
            errors.append(f"步骤 {name} 的产物字节数「{size_s}」非法：须为 1~{MAX_SIZE}")
            continue
        deps = [d.strip() for d in deps_s.split(",") if d.strip()]
        steps.append(Step(name=name, size=size, deps=deps))

    capacity = rerun_limit = 0
    try:
        capacity = int(capacity_s)
        if not 1 <= capacity <= MAX_CAPACITY:
            raise ValueError
    except ValueError:
        errors.append(f"产物占用上限「{capacity_s}」非法：须为 1~{MAX_CAPACITY} 字节")
    try:
        rerun_limit = int(rerun_s)
        if not 0 <= rerun_limit <= 100:
            raise ValueError
    except ValueError:
        errors.append("重跑总次数上限「%s」非法：须为 0~100" % rerun_s)

    result = None
    if errors:
        result = {"kind": "input_error", "errors": errors}
    else:
        try:
            plan, info = solve(steps, target, capacity, rerun_limit)
        except PlanError as exc:
            result = {"kind": "unsolvable", "reason": str(exc)}
        else:
            meta = execute(plan, steps, target, capacity, RUNS_DIR)
            result = {"kind": "executed", "info": info, "meta": meta,
                      "ok": meta["status"] == "success"}

    return _render(rows, target, capacity_s, rerun_s, result=result)


@app.get("/download/<run_id>")
def download(run_id: str):
    if not RUN_ID_RE.fullmatch(run_id):
        abort(404)
    meta_path = os.path.join(RUNS_DIR, run_id + ".json")
    if not os.path.exists(meta_path):
        abort(404)
    with open(meta_path, encoding="utf-8") as fh:
        meta = json.load(fh)
    if meta["status"] != "success" or not meta.get("target_file"):
        abort(410)
    path = os.path.join(RUNS_DIR, run_id, meta["target_file"])
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name=meta["target_file"])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
