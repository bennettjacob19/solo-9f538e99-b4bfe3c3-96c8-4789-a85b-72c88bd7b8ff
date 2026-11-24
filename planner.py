"""容量受限的构建规划器。

给定最多 10 个步骤（名称 / 产物字节数 / 依赖）、目标步骤、产物占用上限、
重跑总次数上限，搜索一个「构建 / 删除」串行方案：

- 构建某步时其全部依赖的产物必须仍在盘上（依赖文件须留到本步写完）；
- 回收（删除）只发生在步骤之间；
- 任意时刻盘上产物总字节数不超过上限（写新产物时保留的文件与未写完的
  文件都占额度，规划时按「构建前占用 + 本步产物全量」计峰值）；
- 被删产物再次用到时须重建，每重建一次计一次重跑，总重跑数不超上限。

搜索目标：字典序最小化 (重跑次数, 构建次数, 删除次数)——非必要不回收。
无解时抛出 PlanError。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import List, Tuple

INF = (10 ** 9, 10 ** 9, 10 ** 9)
MAX_STEPS = 10


@dataclass
class Step:
    name: str
    size: int
    deps: List[str]


class PlanError(Exception):
    """输入非法或无可行方案（调用方应提示且不写任何产物）。"""


def validate_steps(steps: List[Step], target: str) -> None:
    if not steps:
        raise PlanError("至少需要 1 个步骤")
    if len(steps) > MAX_STEPS:
        raise PlanError(f"最多支持 {MAX_STEPS} 个步骤")
    names = [s.name for s in steps]
    if len(set(names)) != len(names):
        raise PlanError("步骤名存在重复")
    name_set = set(names)
    for s in steps:
        if s.size <= 0:
            raise PlanError(f"步骤 {s.name} 的产物字节数必须为正整数")
        for d in s.deps:
            if d == s.name:
                raise PlanError(f"步骤 {s.name} 不能依赖自身")
            if d not in name_set:
                raise PlanError(f"步骤 {s.name} 依赖了未定义的步骤「{d}」")
    if target not in name_set:
        raise PlanError(f"目标步骤「{target}」不在步骤列表中")
    _check_acyclic(steps)


def _check_acyclic(steps: List[Step]) -> None:
    indeg = {s.name: 0 for s in steps}
    adj = {s.name: [] for s in steps}
    for s in steps:
        for d in s.deps:
            adj[d].append(s.name)
            indeg[s.name] += 1
    queue = [n for n, dg in indeg.items() if dg == 0]
    seen = 0
    while queue:
        n = queue.pop()
        seen += 1
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    if seen != len(steps):
        raise PlanError("依赖关系存在环，无法排定构建顺序")


def solve(
    steps: List[Step],
    target: str,
    capacity: int,
    rerun_limit: int,
) -> Tuple[List[Tuple[str, str]], dict]:
    """返回 (方案, 信息)。方案为 [("build"|"delete", 步骤名), ...]。

    无解时抛 PlanError，调用方不得写入任何产物。
    """
    validate_steps(steps, target)
    if capacity <= 0:
        raise PlanError("产物占用上限必须为正整数")
    if rerun_limit < 0:
        raise PlanError("重跑总次数上限不能为负")

    n = len(steps)
    idx = {s.name: i for i, s in enumerate(steps)}
    sizes = [s.size for s in steps]
    dep_mask = [0] * n
    for s in steps:
        for d in s.deps:
            dep_mask[idx[s.name]] |= 1 << idx[d]
    tbit = 1 << idx[target]

    # 每个在场集合的占用（n<=10，直接打表）
    usage = [0] * (1 << n)
    for m in range(1, 1 << n):
        lb = m & -m
        usage[m] = usage[m ^ lb] + sizes[lb.bit_length() - 1]

    # 目标依赖闭包中，若某步「依赖全在场 + 自身产物」都超上限，则永远无解
    closure = tbit
    changed = True
    while changed:
        changed = False
        for i in range(n):
            if closure >> i & 1 and dep_mask[i] & ~closure:
                closure |= dep_mask[i]
                changed = True
    for i in range(n):
        if closure >> i & 1 and usage[dep_mask[i]] + sizes[i] > capacity:
            raise PlanError(
                f"步骤 {steps[i].name} 的依赖加自身产物共 "
                f"{usage[dep_mask[i]] + sizes[i]} 字节，超过上限 {capacity}，"
                "任何顺序都无法构建（未写入任何产物）"
            )

    # Dijkstra：状态=(在场集合, 曾建集合)，代价=(重跑, 构建, 删除)
    start = (0, 0)
    dist = {start: (0, 0, 0)}
    prev = {}
    pq = [((0, 0, 0), 0, 0)]
    goal = None
    while pq:
        cost, present, built = heapq.heappop(pq)
        if dist.get((present, built)) != cost:
            continue
        if present & tbit:
            goal = (present, built)
            break
        # 删除：步骤之间回收；计入代价末位，非必要不删
        m = present
        while m:
            lb = m & -m
            m ^= lb
            ns = (present ^ lb, built)
            nc = (cost[0], cost[1], cost[2] + 1)
            if nc < dist.get(ns, INF):
                dist[ns] = nc
                prev[ns] = ((present, built), ("delete", lb.bit_length() - 1))
                heapq.heappush(pq, (nc, ns[0], ns[1]))
        # 构建：依赖须全在场，占用不得越限；重建计一次重跑
        for i in range(n):
            bit = 1 << i
            if present & bit:
                continue
            if dep_mask[i] & ~present:
                continue
            if usage[present] + sizes[i] > capacity:
                continue
            nc = (cost[0] + (1 if built & bit else 0), cost[1] + 1, cost[2])
            if nc < dist.get(ns := (present | bit, built | bit), INF):
                dist[ns] = nc
                prev[ns] = ((present, built), ("build", i))
                heapq.heappush(pq, (nc, ns[0], ns[1]))

    if goal is None:
        raise PlanError(
            f"容量 {capacity} 字节下不存在可行方案：任何构建/回收顺序都无法"
            "让目标步骤的依赖与产物同时落盘（未写入任何产物）"
        )

    ops = []
    cur = goal
    while cur != start:
        pstate, op = prev[cur]
        ops.append(op)
        cur = pstate
    ops.reverse()
    reruns, builds, deletes = dist[goal]
    if reruns > rerun_limit:
        raise PlanError(
            f"至少需要 {reruns} 次重跑才能建成目标，超过本次上限 "
            f"{rerun_limit}（未写入任何产物）"
        )
    plan = [(op, steps[i].name) for op, i in ops]
    info = {
        "reruns": reruns,
        "builds": builds,
        "deletes": deletes,
        "plan_len": len(plan),
    }
    return plan, info
