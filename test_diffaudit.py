"""diffaudit 核心逻辑冒烟测试（纯 Python，不依赖 fastapi）。"""
import json
import sys

sys.path.insert(0, "/workspace")
from app.diffaudit import compute_audit

V1 = """鉴于甲方拟采购乙方服务，双方约定如下：

第1条 定义
“服务”是指乙方提供的系统集成服务。

第2条 服务内容
乙方应按第1条定义提供“服务”，服务范围见第3条。

第3条 服务期限
服务期为十二个月。

第4条 费用
费用支付见第3条的约定。

第5条 验收
甲方应组织验收。

第6条 保密
双方负有保密义务。

第7条 违约
违约方应承担责任。

第8条 工作时间
本条所称“工作日”为银行营业日。

第8.1条 值班
每个“工作日”均应值班。

第9条 其他
本条款引用第2条。
"""

V2 = """本合同所称“工作日”为除法定节假日外的自然日。

第1条 定义
“服务”是指乙方提供的系统集成服务。

第2条 服务内容
乙方应按第1条定义提供“服务”。

第3条 服务期限
服务期为十二个月。

第4条 费用
费用支付见第1条的约定。

第5条 验收
甲方应组织验收。

第7条 违约
违约方应承担责任。

第6条 保密
双方负有保密义务。

第8条 工作时间
工作时间按法律规定执行。

第8.1条 值班
每个“工作日”均应值班。

第9条 其他
本条款引用第2条。

第10条 生效
本合同自签署之日起生效。
"""

# v2 删除了第3条（c-3），并对换了第6/第7条顺序
order_v2 = ["c-1", "c-2", "c-4", "c-5", "c-7", "c-6", "c-8", "c-8-1", "c-9", "c-10"]
res = compute_audit(V1, [], [], V2, order_v2, ["c-3"])

print("== counts ==")
print(json.dumps(res["counts"], ensure_ascii=False, indent=1))

print("\n== clause_diffs ==")
for d in res["clause_diffs"]:
    la = d["a"]["label"] if d["a"] else "—"
    lb = d["b"]["label"] if d["b"] else "—"
    print(f"  [{d['kind']}] {d['clause_id']} {la} -> {lb}  {d['message']}")

print("\n== ref_diffs ==")
for d in res["ref_diffs"]:
    print(f"  [{d['kind']}] {d['clause_id']} 「{d['text']}」 {d['message']}")

print("\n== term_diffs ==")
for d in res["term_diffs"]:
    print(f"  [{d['kind']}] {d['block_id']} “{d['name']}” {d['message']}")

print("\n== impacts ==")
for r in res["impacts"]:
    chain = " ▸ ".join(
        ("↺" if s["cycle"] else "") + "、".join(m["label"] for m in s["members"])
        + ("（根因）" if s["terminal"] else "")
        for s in r["chain"])
    print(f"  {r['label']} dist={r['distance']} cycle={r['cycle']} 链: {chain}")
    for e in r["events"]:
        print(f"     - [{e['kind']}] {e['message']} roots={e['roots']}")

# ---------------------------------------------------------------- 断言
c = res["counts"]
kinds = {d["clause_id"]: d["kind"] for d in res["clause_diffs"]}
assert kinds["c-2"] == "content", kinds.get("c-2")
assert kinds["c-3"] == "deleted", kinds.get("c-3")
assert kinds["c-4"] == "content", kinds.get("c-4")
assert kinds["c-5"] == "renumbered", kinds.get("c-5")
assert kinds["c-6"] == "moved", kinds.get("c-6")
assert kinds["c-7"] == "moved", kinds.get("c-7")
assert kinds["c-8"] == "content", kinds.get("c-8")
assert kinds["c-8-1"] == "renumbered", kinds.get("c-8-1")
assert kinds["c-9"] == "renumbered", kinds.get("c-9")
assert kinds["c-10"] == "added", kinds.get("c-10")
assert "c-1" not in kinds, "第1条不应有差异"

ref_kinds = [(d["clause_id"], d["kind"]) for d in res["ref_diffs"]]
assert ("c-2", "ref_removed") in ref_kinds, ref_kinds
assert ("c-4", "ref_target_changed") in ref_kinds, ref_kinds

term_kinds = [(d["block_id"], d["kind"]) for d in res["term_diffs"]]
assert ("c-8-1", "term_rebound") in term_kinds, term_kinds

imp = {r["clause_id"]: r for r in res["impacts"]}
assert imp["c-2"]["distance"] == 0
assert imp["c-4"]["distance"] == 0
assert imp["c-8-1"]["distance"] == 0
assert imp["c-9"]["distance"] == 1, imp["c-9"]
# c-9 的链尾根因应为 c-3（被删目标）
last = imp["c-9"]["chain"][-1]
assert last["terminal"] and last["members"][0]["id"] == "c-3", last
# c-2 的根因也是 c-3
last2 = imp["c-2"]["chain"][-1]
assert last2["terminal"] and last2["members"][0]["id"] == "c-3", last2
# c-8-1 的根因是 c-8（定义所在条款，新版仍存活）
last3 = imp["c-8-1"]["chain"][-1]
assert last3["terminal"] and last3["members"][0]["id"] == "c-8", last3

# 稳定性：重算结果完全一致
res2 = compute_audit(V1, [], [], V2, order_v2, ["c-3"])
assert json.dumps(res, ensure_ascii=False, sort_keys=True) == \
       json.dumps(res2, ensure_ascii=False, sort_keys=True)

print("\n✓ 全部断言通过")
