"""版本差异审计：对齐同一草稿的两个已签发版本（不可变快照）。

- 条款按稳定 ID（原路径 slug）对齐，区分 正文改变 / 层级顺序调整 /
  仅编号改写 / 删除 / 新增 / 恢复；
- 引用按 (起点, 原文) 对齐，逐条比较目标集合（区间展开后）与改写表达；
- 术语使用按出现处 ID（块 id + 块内偏移）对齐，比较绑定定义的变化
  （失去唯一绑定 / 改绑 / 变歧义 / 新绑定）；
- 对「目标已删除 / 绑定改变」的差异，在新版存活引用图上按强连通分量
  收缩、沿反向边多源 BFS，给出到根因的最短受影响链（循环组合并为一项）；
- 全部差异稳定排序；审计输入仅为两版快照四要素（正文 / 顺序 / 删除 /
  忽略项），结果由调用方持久化，草稿后续编辑或删除不影响已生成审计。
"""
from __future__ import annotations

from collections import deque

from .parser import parse_contract
from .preview import _scc
from .references import _scan, analyze, cascade_deleted, expand_range
from .terms import review_terms

# 差异类型（展示顺序即稳定排序的次级键）
CLAUSE_KINDS = ("content", "moved", "renumbered", "added", "deleted", "restored")
REF_KINDS = ("ref_target_changed", "ref_expr_changed", "ref_removed", "ref_added")
TERM_KINDS = ("term_lost", "term_rebound", "term_ambiguous", "term_bound")

_INF = 10 ** 9


class _Side:
    """一个版本快照的重分析。

    输入为签发时固定的四要素（正文 / 顺序 / 删除状态），重跑与签发时相同的
    解析、重排、引用改写与术语审校（确定性流程，结果与快照一致），并补充
    审计需要的展开目标集与同父兄弟序列。
    """

    def __init__(self, content: str, order: list[str], deleted: list[str]):
        self.preamble, self.clauses = parse_contract(content)
        known = {c.id for c in self.clauses}
        order = [cid for cid in order if cid in known]
        deleted = [cid for cid in deleted if cid in known]
        self.ref = analyze(self.clauses, order, deleted)
        self.deleted_set = cascade_deleted(self.clauses, deleted)
        self.terms = review_terms(self.preamble, self.clauses, self.deleted_set)

        self.by_id = {c.id: c for c in self.clauses}
        self.orig_path = {c.id: c.path for c in self.clauses}
        self.out = {c["id"]: c for c in self.ref["clauses"]}
        self.alive = [c["id"] for c in self.ref["result"]]
        self.alive_set = set(self.alive)
        # 存活条款在签发文档中的先序位置（0 起），稳定排序与文档序决胜用
        self.pos = {cid: i for i, cid in enumerate(self.alive)}
        self.path_to_id: dict[tuple[int, ...], str] = {}
        for c in self.clauses:
            self.path_to_id.setdefault(c.path, c.id)

        # 引用明细：analyze 的标记与 _scan 的 token 一一对应，补上展开目标集
        self.refs: dict[str, list[dict]] = {}
        for c in self.clauses:
            raws = _scan(c.text)
            items = []
            for r, m in zip(raws, self.out[c.id]["refs"]):
                targets: tuple[str, ...] = ()
                if r.kind == "single":
                    t = self.path_to_id.get(r.a)
                    targets = (t,) if t else ()
                elif r.kind == "range":
                    ids, _ = expand_range(r.a, r.b, self.path_to_id)
                    targets = tuple(ids)
                items.append({
                    "kind": r.kind, "start": r.start, "text": r.text,
                    "new_text": m["new_text"], "status": m["status"],
                    "targets": targets,
                })
            self.refs[c.id] = items

        # 术语使用（occ_id 稳定：块 id + 块内偏移）与定义
        self.uses: dict[str, dict] = {}
        for b in self.terms["blocks"]:
            for mk in b["marks"]:
                if mk["kind"] == "use":
                    self.uses[mk["occ_id"]] = dict(mk, block_id=b["id"])
        self.defs = {d["id"]: d for d in self.terms["definitions"]}

        # 同父存活兄弟（按新顺序）；父级用不变的原路径前缀标识，
        # 与重排后的编号数值无关
        self.siblings: dict[tuple[int, ...], list[str]] = {}
        for cid in self.alive:
            self.siblings.setdefault(self.orig_path[cid][:-1], []).append(cid)

    def loc(self, cid: str) -> dict | None:
        """单侧定位信息：存活时给签发编号与文档位置，已删除给原编号。"""
        if cid not in self.by_id:
            return None
        o = self.out[cid]
        alive = cid in self.alive_set
        return {
            "alive": alive,
            "label": o["new_label"] if alive else o["label"],
            "position": self.pos.get(cid),
        }

    def label_of(self, cid: str) -> str:
        loc = self.loc(cid)
        return loc["label"] if loc else cid


# ---------------------------------------------------------------- 条款差异

def _order_changed(a: _Side, b: _Side, cid: str) -> bool:
    """该条款与同父共同存活兄弟的相对顺序是否变化。

    父级用不变的原路径前缀标识；只比较该条款的前驱 / 后继共同兄弟集合，
    与兄弟的增删以及其他位置无关条款的换序互不影响。
    """
    parent = a.orig_path[cid][:-1]
    sa = a.siblings.get(parent, [])
    sb = b.siblings.get(parent, [])
    common = (a.alive_set & b.alive_set) - {cid}
    ia, ib = sa.index(cid), sb.index(cid)
    before_a = {x for x in sa[:ia] if x in common}
    before_b = {x for x in sb[:ib] if x in common}
    after_a = {x for x in sa[ia + 1:] if x in common}
    after_b = {x for x in sb[ib + 1:] if x in common}
    return before_a != before_b or after_a != after_b


def _clause_diffs(a: _Side, b: _Side) -> list[dict]:
    diffs: list[dict] = []
    for cid in set(a.by_id) | set(b.by_id):
        in_a, in_b = cid in a.by_id, cid in b.by_id
        alive_a, alive_b = cid in a.alive_set, cid in b.alive_set
        heading = ((b.out[cid]["heading"] if in_b else a.out[cid]["heading"])
                   or "")
        kind: str | None = None
        if not in_a:
            kind, message = "added", "新版新增条款"
        elif not in_b:
            kind, message = "deleted", "新版正文中已移除该条款"
        elif alive_a and not alive_b:
            kind, message = "deleted", "新版中已删除（含级联后代）"
        elif not alive_a and alive_b:
            kind, message = "restored", "新版中恢复为存活条款"
        elif not alive_a and not alive_b:
            continue  # 两版均处于删除状态，无变化
        elif a.out[cid]["before_text"].strip() != b.out[cid]["before_text"].strip():
            kind, message = "content", "正文已修改"
        elif _order_changed(a, b, cid):
            kind, message = "moved", "同父兄弟间的相对顺序发生变化"
        elif a.out[cid]["new_label"] != b.out[cid]["new_label"]:
            kind, message = "renumbered", "内容与相对顺序未变，仅编号随兄弟增删改写"
        else:
            continue
        la, lb = a.loc(cid), b.loc(cid)
        if la and lb and la["label"] != lb["label"]:
            message += f"（{la['label']} → {lb['label']}）"
        diffs.append({
            "clause_id": cid, "kind": kind, "heading": heading,
            "message": message, "a": la, "b": lb,
        })
    order = {k: i for i, k in enumerate(CLAUSE_KINDS)}
    diffs.sort(key=lambda d: (
        min(a.pos.get(d["clause_id"], _INF), b.pos.get(d["clause_id"], _INF)),
        order[d["kind"]], d["clause_id"]))
    return diffs


# ---------------------------------------------------------------- 引用差异

def _target_labels(side: _Side, targets) -> str:
    return "、".join(side.label_of(t) for t in targets) or "（无）"


def _ref_diffs(a: _Side, b: _Side) -> list[dict]:
    """逐条比较两版均存活条款的引用：目标集合、区间表达、新增 / 移除。

    引用按条款内起点偏移对齐：同一偏移上的引用视为同一引用的两版形态
    （正文编辑只改写编号文本时仍能配成「目标改变」，而非「移除 + 新增」）。
    """
    diffs: list[dict] = []
    both_alive = sorted(
        a.alive_set & b.alive_set,
        key=lambda c: (min(a.pos[c], b.pos[c]), c))
    for cid in both_alive:
        ra = {r["start"]: r for r in a.refs[cid]}
        rb = {r["start"]: r for r in b.refs[cid]}
        for start in sorted(set(ra) | set(rb)):
            x, y = ra.get(start), rb.get(start)
            if x is not None and y is not None:
                ref_name = (f"「{x['text']}」" if x["text"] == y["text"]
                            else f"「{x['text']}」→「{y['text']}」")
                if set(x["targets"]) != set(y["targets"]):
                    kind = "ref_target_changed"
                    message = (f"引用{ref_name}的目标由 "
                               f"{_target_labels(a, x['targets'])} 变为 "
                               f"{_target_labels(b, y['targets'])}")
                elif x["new_text"] != y["new_text"]:
                    kind = "ref_expr_changed"
                    message = (f"引用{ref_name}的区间表达由「{x['new_text']}」"
                               f"变为「{y['new_text']}」")
                else:
                    continue
                diffs.append({
                    "clause_id": cid, "kind": kind, "start": x["start"],
                    "text": x["text"], "text_b": y["text"],
                    "expr_a": x["new_text"], "expr_b": y["new_text"],
                    "targets_a": list(x["targets"]),
                    "targets_b": list(y["targets"]),
                    "message": message,
                    "a": a.loc(cid), "b": b.loc(cid),
                })
            elif x is not None:
                gone = [t for t in x["targets"]
                        if t not in b.by_id or t in b.deleted_set]
                note = (f"；原目标 {_target_labels(a, gone)} 在新版已删除"
                        if gone else "；目标仍存活，引用随正文编辑移除")
                diffs.append({
                    "clause_id": cid, "kind": "ref_removed",
                    "start": x["start"], "text": x["text"], "text_b": None,
                    "expr_a": x["new_text"], "expr_b": None,
                    "targets_a": list(x["targets"]), "targets_b": [],
                    "message": f"引用「{x['text']}」在新版中移除{note}",
                    "a": a.loc(cid), "b": b.loc(cid),
                })
            else:
                diffs.append({
                    "clause_id": cid, "kind": "ref_added",
                    "start": y["start"], "text": y["text"], "text_b": None,
                    "expr_a": None, "expr_b": y["new_text"],
                    "targets_a": [], "targets_b": list(y["targets"]),
                    "message": (f"新版新增引用「{y['text']}」"
                                f"（目标 {_target_labels(b, y['targets'])}）"),
                    "a": a.loc(cid), "b": b.loc(cid),
                })
    return diffs


# ---------------------------------------------------------------- 术语绑定差异

def _def_where(side: _Side, def_id: str | None) -> dict | None:
    d = side.defs.get(def_id) if def_id else None
    if not d:
        return None
    bid = d["block_id"]
    return {
        "def_id": def_id, "block_id": bid,
        "where": "前置说明" if bid == "preamble" else side.label_of(bid),
        "scope_label": d["scope_label"],
    }


def _term_diffs(a: _Side, b: _Side) -> list[dict]:
    """按出现处 ID 对齐术语使用，比较唯一绑定的变化。"""
    diffs: list[dict] = []
    for occ in set(a.uses) & set(b.uses):
        ua, ub = a.uses[occ], b.uses[occ]
        ok_a = ua["status"] == "ok" and len(ua.get("target_ids", [])) == 1
        ok_b = ub["status"] == "ok" and len(ub.get("target_ids", [])) == 1
        da = ua["target_ids"][0] if ok_a else None
        db = ub["target_ids"][0] if ok_b else None
        if da and db and da == db:
            continue
        if da and db:
            kind = "term_rebound"
        elif da and ub["status"] == "undefined":
            kind = "term_lost"
        elif da and ub["status"] == "ambiguous":
            kind = "term_ambiguous"
        elif not da and db:
            kind = "term_bound"
        else:
            continue  # 两侧均无唯一绑定（未定义 / 歧义之间迁移）不计绑定改变
        wa, wb = _def_where(a, da), _def_where(b, db)
        name = ua["name"]
        if kind == "term_rebound":
            message = (f"术语“{name}”的绑定由「{wa['where']}定义」"
                       f"改为「{wb['where']}定义」")
        elif kind == "term_lost":
            message = (f"术语“{name}”的唯一绑定（{wa['where']}定义）"
                       f"在新版失效，本处变为未定义")
        elif kind == "term_ambiguous":
            message = (f"术语“{name}”原绑定{wa['where']}定义，"
                       f"新版命中多个同名定义，变为歧义绑定")
        else:
            message = f"术语“{name}”在新版获得唯一绑定（{wb['where']}定义）"
        owner = ua["block_id"]
        if owner == "preamble":
            loc = {"alive": True, "label": "前置说明", "position": -1}
            la = lb = loc
        else:
            la, lb = a.loc(owner), b.loc(owner)
        diffs.append({
            "occ_id": occ, "block_id": owner, "name": name, "kind": kind,
            "def_a": da, "def_b": db, "where_a": wa, "where_b": wb,
            "start": ua["start"], "message": message, "a": la, "b": lb,
        })

    def sort_key(d: dict):
        owner = d["block_id"]
        pos = -1 if owner == "preamble" else min(
            a.pos.get(owner, _INF), b.pos.get(owner, _INF))
        return (pos, d["start"], d["occ_id"])

    diffs.sort(key=sort_key)
    return diffs


# ---------------------------------------------------------------- 受影响链（新版存活引用图）

def _build_impacts(a: _Side, b: _Side,
                   ref_diffs: list[dict], term_diffs: list[dict]) -> list[dict]:
    """对「目标已删除 / 绑定改变」的差异，在新版存活引用图上求最短受影响链。

    与删除影响预演同一套图算法：强连通分量收缩为 DAG → 直接事件按分量
    归并（引用类同根因集去重，术语按出现处保留）→ 反向边多源 BFS 求
    传播距离 → 等长链按文档顺序取字典序最小者 → 链尾追加根因条款。
    """
    events: list[dict] = []
    for d in ref_diffs:
        if d["kind"] == "ref_added":
            continue  # 新增引用不是「既有绑定改变」
        lost = [t for t in d["targets_a"] if t not in set(d["targets_b"])]
        roots = sorted({t for t in lost
                        if t not in b.by_id or t in b.deleted_set},
                       key=lambda t: a.pos.get(t, _INF))
        if not roots:
            # 目标仍存活：正文编辑 / 结构调整导致的改绑或移除，根因为条款自身
            roots = [d["clause_id"]]
        events.append({
            "owner": d["clause_id"], "kind": d["kind"],
            "message": d["message"], "roots": roots, "start": d["start"],
        })
    for d in term_diffs:
        if d["kind"] == "term_bound":
            continue  # 新绑定（含改善）不是「既有绑定改变」
        root = None
        if d["def_a"] and d["def_a"] in a.defs:
            bid = a.defs[d["def_a"]]["block_id"]
            root = bid if bid != "preamble" else None
        owner = d["block_id"]
        events.append({
            "owner": owner, "kind": d["kind"], "message": d["message"],
            "roots": [root] if root else [owner], "start": d["start"],
        })
    if not events:
        return []

    # 新版存活引用图（单条自指计边、区间自指不计，与删除预演一致）
    edges: dict[str, set[str]] = {cid: set() for cid in b.alive}
    for cid in b.alive:
        for r in b.refs[cid]:
            if r["status"] != "ok":
                continue
            for t in r["targets"]:
                if t in b.alive_set and not (r["kind"] == "range" and t == cid):
                    edges[cid].add(t)

    comp = _scc(b.alive, edges, b.pos)
    comp_members: dict[int, list[str]] = {}
    for cid in b.alive:
        comp_members.setdefault(comp[cid], []).append(cid)
    for mem in comp_members.values():
        mem.sort(key=lambda x: b.pos[x])
    comps = sorted(comp_members, key=lambda k: b.pos[comp_members[k][0]])
    cdoc = {k: b.pos[comp_members[k][0]] for k in comps}
    cyclic = {k: len(m) > 1 or m[0] in edges[m[0]]
              for k, m in comp_members.items()}

    cedges: dict[int, set[int]] = {k: set() for k in comps}
    for u, vs in edges.items():
        for v in vs:
            if comp[u] != comp[v]:
                cedges[comp[u]].add(comp[v])

    # 直接事件按分量归并：引用类同根因集去重，术语按出现处保留
    direct: dict[int, list[dict]] = {}
    seen: dict[int, set] = {}
    preamble_events: list[dict] = []
    for ev in events:
        if ev["owner"] == "preamble":
            preamble_events.append(ev)
            continue
        k = comp[ev["owner"]]
        if ev["kind"].startswith("ref_"):
            key = (ev["kind"], tuple(sorted(ev["roots"])))
            if key in seen.setdefault(k, set()):
                continue
            seen[k].add(key)
        direct.setdefault(k, []).append(ev)

    # 根分量 -> 根因条款（分量成员自身除外；根因为自身时链即分量本身）
    root_terminals: dict[int, list[str]] = {}
    for k, evs in direct.items():
        member_set = set(comp_members[k])
        roots = sorted({r for ev in evs for r in ev["roots"]
                        if r not in member_set},
                       key=lambda t: min(a.pos.get(t, _INF),
                                         b.pos.get(t, _INF)))
        if roots:
            root_terminals[k] = roots

    # 收缩后的 DAG 上多源 BFS（反向边）求最短距离
    crev: dict[int, list[int]] = {k: [] for k in comps}
    for u, vs in cedges.items():
        for v in vs:
            crev[v].append(u)
    for vs in crev.values():
        vs.sort(key=lambda x: cdoc[x])

    dist: dict[int, int] = {k: 0 for k in direct}
    dq: deque[int] = deque(sorted(direct, key=lambda x: cdoc[x]))
    while dq:
        cur = dq.popleft()
        for nxt in crev[cur]:
            if nxt not in dist:
                dist[nxt] = dist[cur] + 1
                dq.append(nxt)

    # 等长链取文档顺序字典序最小者
    best: dict[int, list[int]] = {k: [k] for k in direct}
    for level in range(1, max(dist.values(), default=0) + 1):
        layer = sorted((n for n in dist if dist[n] == level),
                       key=lambda x: cdoc[x])
        for n in layer:
            preds = [p for p in cedges[n] if dist.get(p) == level - 1]
            chosen = min(preds, key=lambda p: [cdoc[q] for q in best[p]])
            best[n] = [n] + best[chosen]

    def brief(side: _Side, cid: str) -> dict:
        loc = side.loc(cid)
        c = side.by_id.get(cid)
        return {
            "id": cid,
            "label": loc["label"] if loc else cid,
            "heading": (c.heading if c else "") or "",
        }

    def step_for(k: int, terminal: bool = False) -> dict:
        mem = comp_members[k]
        return {
            "kind": "group" if len(mem) > 1 else "clause",
            "cycle": cyclic[k], "terminal": terminal,
            "members": [brief(b, x) for x in mem],
        }

    def terminal_step(tid: str) -> dict | None:
        if tid in comp:
            return step_for(comp[tid], terminal=True)
        if tid in a.by_id or tid in b.by_id:
            side = b if tid in b.by_id else a
            return {"kind": "clause", "cycle": False, "terminal": True,
                    "members": [brief(side, tid)]}
        return None

    impacts: list[dict] = []
    for k in sorted(dist, key=lambda x: (dist[x], cdoc[x])):
        mem = comp_members[k]
        evs = sorted(direct.get(k, []),
                     key=lambda e: (b.pos.get(e["owner"], _INF),
                                    e["start"], e["kind"]))
        chain = [step_for(x) for x in best[k]]
        root_k = best[k][-1]
        for tid in root_terminals.get(root_k, []):
            step = terminal_step(tid)
            if step is not None:
                chain.append(step)

        if len(mem) > 1:
            label = "循环组（" + "、".join(b.label_of(x) for x in mem) + "）"
            headings = [b.out[x]["heading"] for x in mem
                        if b.out[x]["heading"]]
            heading = " ↔ ".join(dict.fromkeys(headings))
        else:
            label = b.label_of(mem[0])
            heading = b.out[mem[0]]["heading"] or ""

        impacts.append({
            "clause_id": mem[0], "member_ids": list(mem),
            "cycle_group": len(mem) > 1, "cycle": cyclic[k],
            "label": label, "heading": heading, "distance": dist[k],
            "chain": chain,
            "events": [{
                "kind": e["kind"], "message": e["message"],
                "owner_id": e["owner"], "roots": list(e["roots"]),
            } for e in evs],
        })

    # 前置说明中的术语绑定变化（不能成为引用边节点，独立成行）
    if preamble_events:
        chain = [{
            "kind": "preamble", "cycle": False, "terminal": False,
            "members": [{"id": "preamble", "label": "前置说明", "heading": ""}],
        }]
        roots = sorted({r for e in preamble_events for r in e["roots"]},
                       key=lambda t: min(a.pos.get(t, _INF),
                                         b.pos.get(t, _INF)))
        for tid in roots:
            step = terminal_step(tid)
            if step is not None:
                chain.append(step)
        impacts.insert(0, {
            "clause_id": "preamble", "member_ids": ["preamble"],
            "cycle_group": False, "cycle": False,
            "label": "前置说明", "heading": "", "distance": 0,
            "chain": chain,
            "events": [{
                "kind": e["kind"], "message": e["message"],
                "owner_id": None, "roots": list(e["roots"]),
            } for e in preamble_events],
        })
    return impacts


# ---------------------------------------------------------------- 主入口

def compute_audit(content_a: str, order_a: list[str], deleted_a: list[str],
                  content_b: str, order_b: list[str],
                  deleted_b: list[str]) -> dict:
    """比较两个版本快照（A 旧 → B 新），返回稳定排序的差异审计结果。"""
    a = _Side(content_a, order_a, deleted_a)
    b = _Side(content_b, order_b, deleted_b)

    clause_diffs = _clause_diffs(a, b)
    ref_diffs = _ref_diffs(a, b)
    term_diffs = _term_diffs(a, b)
    impacts = _build_impacts(a, b, ref_diffs, term_diffs)

    counts = {k: 0 for k in CLAUSE_KINDS + REF_KINDS + TERM_KINDS}
    for d in clause_diffs:
        counts[d["kind"]] += 1
    for d in ref_diffs:
        counts[d["kind"]] += 1
    for d in term_diffs:
        counts[d["kind"]] += 1
    counts["impacts"] = len(impacts)
    counts["cycles"] = sum(1 for r in impacts if r["cycle"])
    counts["total"] = len(clause_diffs) + len(ref_diffs) + len(term_diffs)

    return {
        "counts": counts,
        "clause_diffs": clause_diffs,
        "ref_diffs": ref_diffs,
        "term_diffs": term_diffs,
        "impacts": impacts,
    }
