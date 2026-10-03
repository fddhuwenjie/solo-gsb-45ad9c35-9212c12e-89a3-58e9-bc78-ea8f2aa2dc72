"""diffaudit 边界场景测试：区间表达 / 术语失绑 / 恢复 / 新增插入。"""
import json
import sys

sys.path.insert(0, "/workspace")
from app.diffaudit import compute_audit

# ---- 场景 1：区间表达被拆散（目标集不变，新位置不再连续） ----
A1 = """第1条 总则
服务范围见第2条至第4条。

第2条 甲
内容甲。

第3条 乙
内容乙。

第4条 丙
内容丙。

第5条 丁
内容丁。
"""
# v2：把第5条插到第2、第3条之间 → 区间目标新位置 (2,)(4,)(5,) 不再连续
B1 = A1
res = compute_audit(A1, [], [], B1, ["c-1", "c-2", "c-5", "c-3", "c-4"], [])
expr = [d for d in res["ref_diffs"] if d["kind"] == "ref_expr_changed"]
assert len(expr) == 1, res["ref_diffs"]
assert expr[0]["expr_a"] == "第2条至第4条", expr[0]
assert expr[0]["expr_b"] == "第2条、第4条至第5条", expr[0]
print("✓ 场景1 区间表达拆散:", expr[0]["message"])

# ---- 场景 2：术语失去唯一定义（新版删除定义所在条款） ----
A2 = """第1条 定义
“服务”是指系统集成服务。

第2条 内容
乙方应提供“服务”。
"""
B2 = A2  # 正文相同，仅删除第1条
res2 = compute_audit(A2, [], [], B2, [], ["c-1"])
tl = [d for d in res2["term_diffs"] if d["kind"] == "term_lost"]
assert len(tl) == 1, res2["term_diffs"]
imp = res2["impacts"]
assert len(imp) == 1 and imp[0]["clause_id"] == "c-2", imp
last = imp[0]["chain"][-1]
assert last["terminal"] and last["members"][0]["id"] == "c-1", last
print("✓ 场景2 术语失绑:", tl[0]["message"], "| 链:",
      " ▸ ".join(m["label"] for s in imp[0]["chain"] for m in s["members"]))

# ---- 场景 3：恢复条款 + 新增插入中间 ----
A3 = """第1条 甲
内容甲。

第2条 乙
内容乙。

第3条 丙
内容丙。
"""
B3 = """第1条 甲
内容甲。

第2条 乙
内容乙。

第3条 丙
内容丙。

第4条 丁
内容丁。
"""
# v1 删除 c-2；v2 恢复 c-2 并在末尾新增 c-4（c-3 编号不变）
res3 = compute_audit(A3, [], ["c-2"], B3, [], [])
kinds = {d["clause_id"]: d["kind"] for d in res3["clause_diffs"]}
assert kinds["c-2"] == "restored", kinds
assert kinds["c-4"] == "added", kinds
# c-3 因兄弟 c-2 恢复而编号漂移（第2条 → 第3条），属「仅编号改写」
assert kinds["c-3"] == "renumbered", kinds
assert "c-1" not in kinds, kinds
print("✓ 场景3 恢复/新增:", {k: v for k, v in kinds.items()})

# ---- 场景 4：循环组防御路径（合成数据，签发门禁外的理论情形） ----
# 直接验证 _build_impacts 在成环图上合并循环组
from app.diffaudit import _Side, _build_impacts

CYC = """第1条 甲
见第2条。

第2条 乙
见第1条。

第3条 丙
见第1条。

第4条 丁
见第3条。
"""
side_a = _Side(CYC, [], [])
side_b = _Side(CYC, [], [])
# 伪造一个直接事件：c-1 的引用目标改变，根因 c-9（不存在于 B → terminal 跳过）
ref_diffs = [{
    "clause_id": "c-1", "kind": "ref_target_changed", "start": 0,
    "text": "第2条", "targets_a": ["c-2"], "targets_b": [],
    "message": "测试事件",
}]
# c-2 在 B 存活 → 根因为 c-2 自身所在分量（与 c-1 同循环组）→ 组内根因不外挂
imp4 = _build_impacts(side_a, side_b, ref_diffs, [])
groups = [r for r in imp4 if r["cycle_group"]]
assert len(groups) == 1, imp4  # c-1/c-2 循环组合并为一个影响项
assert set(groups[0]["member_ids"]) == {"c-1", "c-2"}, groups[0]
# c-3 引用 c-1 → 连锁 1 跳；c-4 引用 c-3 → 连锁 2 跳
dist = {r["clause_id"]: r["distance"] for r in imp4}
assert dist.get("c-3") == 1 and dist.get("c-4") == 2, dist
print("✓ 场景4 循环组合并:", [(r["label"], r["distance"]) for r in imp4])

print("\n✓ 全部边界场景通过")
