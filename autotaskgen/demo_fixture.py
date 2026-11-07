#!/usr/bin/env python3
"""Write a realistic demo store for the progress board (screenshots, manual checks).

  PROGRESS_HOME=/tmp/pb python3 demo_fixture.py [--home /tmp/pb] [--stale-live]

Creates project `demo` (2 cores, 6 lines, ~40 nodes in every status, 5 milestones that are
confirmed / claimed / open, a live.json with workers on wsl and mac, ib-agent unknown) and a
small second project `paper` so the home page shows more than one card. Every event goes
through progress_core.append, so it passes the same validation as real writes.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import pathlib
import shutil
import sys
import zlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import progress_core as core  # noqa: E402

C1 = "term_d7cf403c-demo-core1"
C2 = "term_6705ca88-demo-core2"
HUMAN = "term_human-demo"
ROUTERS = {
    "EVB": "term_7d3ec177-demo", "BASE": "term_02e8c8c5-demo", "SR": "term_10c91747-demo",
    "FE": "term_633f0369-demo", "AG": "term_24c1be86-demo", "KB": "term_6ddec706-demo",
    "ACC": "term_1d80cfcf-demo", "DB": "term_cbfbc4e7-demo",
}
MODELS = ["codex/gpt-6.1-sol", "codex/gpt-6.1-terra", "codex/gpt-6-luna",
          "lunaroute/deepseek-4.1-flash", "claude/opus-5.5"]

NOW = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def t(minutes_ago: float) -> str:
    return (NOW - _dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def project_doc():
    return {
        "schema": core.SCHEMA_PROJECT, "id": "demo", "name": "通用软硬件协同 DSE 循环（演示）",
        "final_goal": {"text": "给定一个新应用，Agent 自己跑完 设计空间探索 → 评估 → 选型 的闭环",
                       "done_when": "两个未见过的应用上，闭环选出的方案进入人工基线前 10%"},
        "humans": [HUMAN],
        "cores": [{"id": "core1", "name": "核心一 性能评估", "coordinator": C1},
                  {"id": "core2", "name": "核心二 Agent 闭环", "coordinator": C2}],
        "lines": [
            {"id": "EVB", "name": "事件级基线评估器", "core": "core1", "router": ROUTERS["EVB"], "patrol": True},
            {"id": "BASE", "name": "基线数据与校准", "core": "core1", "router": ROUTERS["BASE"], "patrol": False},
            {"id": "SR", "name": "结构关系建模", "core": "core1", "router": ROUTERS["SR"], "patrol": True},
            {"id": "ACC", "name": "混合评估器（ACC）：早先的核心一主体，近几天没有新节点", "core": "core1",
             "router": ROUTERS["ACC"], "patrol": False},
            {"id": "DB", "name": "评估数据集（DB）：带标签的设计和性能样本", "core": "core1",
             "router": ROUTERS["DB"], "patrol": False},
            {"id": "FE", "name": "前端 Agent 搜索", "core": "core2", "router": ROUTERS["FE"], "patrol": True},
            {"id": "AG", "name": "Agent 决策回路", "core": "core2", "router": ROUTERS["AG"], "patrol": True},
            {"id": "KB", "name": "知识库与检索", "core": "core2", "router": ROUTERS["KB"], "patrol": True},
        ],
        "milestones": [
            {"id": "M1", "title": "评估器首个闭环通过", "core": "core1", "lines": ["EVB", "BASE"], "after": [],
             "done_when": "EVB 在 3 个基准上误差 < 15%"},
            {"id": "M2", "title": "前端接口冻结", "core": "core2", "lines": ["FE"], "after": [],
             "done_when": "评估器接口 v1 双方签字"},
            {"id": "M3", "title": "结构关系可预测", "core": "core1", "lines": ["SR"], "after": ["M1"],
             "done_when": "留出集排序相关 > 0.8"},
            {"id": "M4", "title": "Agent 自主跑通一轮", "core": "core2", "lines": ["AG", "FE"], "after": ["M2", "M3"],
             "done_when": "无人工干预跑完一轮探索"},
            {"id": "M5", "title": "新应用上进入前 10%", "core": "core2", "lines": ["AG", "KB"], "after": ["M4"],
             "done_when": "两个未见应用都达标"},
        ],
    }


class Writer:
    def __init__(self, pid):
        self.pid = pid
        self.n = 0

    def add(self, kind, role, handle, at, **kw):
        ev = {"kind": kind, "actor": {"role": role, "handle": handle}, "at": at,
              "summary": kw.pop("summary")}
        model = kw.pop("model", None)
        if model:
            ev["actor"]["model"] = model
        ev.update(kw)
        self.n += 1
        return core.append(self.pid, ev)


def node_life(w, line, nid, title, start, steps, deps=(), milestones=(), model=None,
              role="router", source=None, expected=None, node_kind=None):
    """steps: list of (kind, minutes_after_start, extra) applied in order."""
    r = ROUTERS[line]
    who = (role, r if role == "router" else "import")
    extra = {"source": source} if source else {}
    plan = dict(line=line, node=nid, title=title, deps=list(deps), summary="排好：" + title,
                milestones=list(milestones), **extra)
    if model and role == "router":
        plan["spec"] = "you/%s-dev@docs/validation/router-%s/specs/%s.md" % (line.lower(), line, nid)
        plan["route"] = {"agent": "codex", "model": model, "effort": "high", "on": "wsl"}
    if expected:
        plan["expected_duration_minutes"] = expected
    if node_kind:
        plan["node_kind"] = node_kind
    w.add("node_plan", who[0], who[1], t(start), **plan)
    worker = "term_w%s-%s" % (zlib.crc32(nid.encode()) % 10 ** 6, line.lower())
    for kind, dt, kw in steps:
        at = t(start - dt)
        kw = dict(kw)
        summ = kw.pop("summary", None)
        if kind == "dispatch":
            w.add("node_dispatch", who[0], who[1], at, line=line, node=nid, model=model,
                  summary=summ or "已派出 worker", route={"model": model} if model else None,
                  refs={"terminal": worker, "worktree": "%s-%s" % (line.lower(), nid),
                        "dispatch": "ctx_%s" % nid[:8]}, **extra)
        elif kind == "deliver":
            hr = (who[0], who[1]) if role == "import" else ("worker", worker)
            w.add("node_deliver", hr[0], hr[1], at, line=line, node=nid, model=model,
                  outcome=kw.pop("outcome"), summary=summ or "交付",
                  refs={"report": "docs/validation/router-%s/%s/REPORT.md" % (line, nid),
                        "head": "%07x" % (zlib.crc32((nid + "h").encode()) % 16 ** 7)}, **kw, **extra)
        elif kind == "review":
            w.add("node_review", who[0], who[1], at, line=line, node=nid,
                  outcome=kw.pop("outcome"), summary=summ or "审查", **extra)
        elif kind == "block":
            hr = (who[0], who[1]) if role == "import" else ("worker", worker)
            w.add("node_block", hr[0], hr[1], at, line=line, node=nid,
                  blocker=kw.pop("blocker"), summary=summ or "卡住", **extra)
        elif kind == "heartbeat":
            w.add("node_heartbeat", "worker", worker, at, line=line, node=nid, summary=summ or "仍在跑")
        elif kind == "supersede":
            w.add("node_supersede", who[0], who[1], at, line=line, node=nid, by=kw.pop("by"),
                  summary=summ or "被修复节点取代", **extra)
        elif kind == "cancel":
            w.add("node_cancel", who[0], who[1], at, line=line, node=nid, summary=summ or "取消", **extra)
    return worker


def build_demo(w):
    S, T, L, F, O = MODELS
    # ---- EVB: the busiest line, M1 confirmed
    w.add("line_open", "coordinator", C1, t(3000), line="EVB", summary="开 EVB 主线")
    node_life(w, "EVB", "evb_trace_ingest", "读入事件轨迹", 2900, [
        ("dispatch", 10, {}), ("deliver", 300, {"outcome": "PASS", "summary": "三种轨迹格式都能读入"}),
        ("review", 360, {"outcome": "PASS", "summary": "抽查 20 条，格式正确"})], model=T, milestones=["M1"])
    node_life(w, "EVB", "evb_cache_model", "缓存层次模型", 2700, [
        ("dispatch", 20, {}), ("deliver", 400, {"outcome": "PASS", "result": "negative", "summary": "L1/L2 对上，L3 偏差 22%"}),
        ("review", 460, {"outcome": "PARTIAL", "summary": "L3 偏差记入下一节点"})],
        deps=["evb_trace_ingest"], model=S, milestones=["M1"])
    node_life(w, "EVB", "evb_mem_stream", "内存流水模型", 2500, [
        ("dispatch", 30, {}), ("deliver", 500, {"outcome": "PASS", "result": "negative", "summary": "带宽饱和时误差 60%，模型结构不对"})],
        deps=["evb_cache_model"], model=L)
    node_life(w, "EVB", "evb_mem_stream_repair", "内存流水模型（修复）", 1900, [
        ("dispatch", 10, {}), ("deliver", 380, {"outcome": "PASS", "summary": "改成排队模型后误差 9%"}),
        ("review", 420, {"outcome": "PASS", "summary": "三个基准都在 15% 内"})],
        deps=["evb_cache_model"], model=S, milestones=["M1"])
    w.add("node_supersede", "router", ROUTERS["EVB"], t(1890), line="EVB", node="evb_mem_stream",
          by="evb_mem_stream_repair", summary="由修复节点取代")
    w.add("milestone_claim", "router", ROUTERS["EVB"], t(1460), line="EVB", milestone="M1",
          summary="三个基准误差都在 15% 内", refs={"report": "docs/validation/router-EVB/M1_CLAIM.md"})
    w.add("milestone_confirm", "coordinator", C1, t(1300), milestone="M1", summary="复核三份报告，确认达成")
    node_life(w, "EVB", "evb_multi_core", "多核扩展", 900, [
        ("dispatch", 15, {}), ("heartbeat", 400, {}), ("heartbeat", 700, {"summary": "8 核场景跑到一半"})],
        deps=["evb_mem_stream_repair"], model=S, milestones=["M3"], expected=600)
    node_life(w, "EVB", "evb_noc_model", "片上网络模型", 700, [
        ("dispatch", 10, {}), ("deliver", 640, {"outcome": "PASS", "summary": "网格拓扑延迟对上，误差 7%"})],
        deps=["evb_mem_stream_repair"], model=T)
    node_life(w, "EVB", "evb_power", "功耗估计", 400, [], deps=["evb_multi_core"], model=T)
    node_life(w, "EVB", "evb_calib_review", "评估器整体审查", 300, [("dispatch", 10, {}), ("heartbeat", 120, {})],
              deps=["evb_noc_model"], model=O, expected=30, node_kind="review")

    # ---- BASE: imported from old graph files, partly quiet
    w.add("line_open", "import", "import", t(5000), line="BASE", summary="导入：基线主线", source="import")
    node_life(w, "BASE", "base_bench_set", "基准程序集", 4900, [
        ("dispatch", 10, {}), ("deliver", 600, {"outcome": "PASS", "summary": "12 个基准入库"}),
        ("review", 700, {"outcome": "PASS", "summary": "导入：已审"})], role="import", source="import",
        model=T, milestones=["M1"])
    node_life(w, "BASE", "base_gem5_ref", "gem5 参考数据", 4500, [
        ("dispatch", 10, {}), ("deliver", 900, {"outcome": "PARTIAL", "summary": "9/12 跑完，3 个超时"})],
        role="import", source="import", deps=["base_bench_set"], model=S, milestones=["M1"])
    node_life(w, "BASE", "base_gem5_rest", "补跑超时的 3 个", 3200, [
        ("dispatch", 30, {}), ("block", 300, {"blocker": "resource", "summary": "虚拟机内存不够，等扩容"})],
        role="import", source="import", deps=["base_gem5_ref"], model=L)
    node_life(w, "BASE", "base_hw_counters", "硬件计数器采集", 3000, [
        ("cancel", 100, {"summary": "改用 gem5 数据，不采真机"})], role="import", source="import")
    node_life(w, "BASE", "base_calib", "校准系数", 2000, [
        ("dispatch", 10, {}), ("deliver", 500, {"outcome": "PASS", "summary": "系数拟合完成"})],
        role="import", source="import", deps=["base_gem5_ref"], model=T)
    node_life(w, "BASE", "base_report", "基线报告", 1000, [], role="import", source="import",
              deps=["base_gem5_rest"])

    # ---- SR: M3 claimed, waiting for coordinator
    w.add("line_open", "coordinator", C1, t(2400), line="SR", summary="开 SR 主线")
    node_life(w, "SR", "sr_feature_set", "结构特征表", 2300, [
        ("dispatch", 10, {}), ("deliver", 300, {"outcome": "PASS", "summary": "48 维特征表就绪"}),
        ("review", 340, {"outcome": "PASS", "summary": "特征无泄漏"})], model=L, milestones=["M3"])
    node_life(w, "SR", "sr_rank_model", "排序模型", 2000, [
        ("dispatch", 10, {}), ("deliver", 600, {"outcome": "PASS", "summary": "留出集排序相关 0.84"}),
        ("review", 700, {"outcome": "PASS", "summary": "复算一致"})], deps=["sr_feature_set"], model=S,
        milestones=["M3"])
    node_life(w, "SR", "sr_ablation", "特征消融", 1200, [
        ("dispatch", 10, {}), ("deliver", 500, {"outcome": "BLOCKED", "blocker": "design", "blocked_by": "两组结果矛盾，原因未明，要先定位",
                          "summary": "两组结果矛盾，原因未明"})],
        deps=["sr_rank_model"], model=F)
    node_life(w, "SR", "sr_transfer", "跨应用迁移", 800, [
        ("dispatch", 20, {}), ("heartbeat", 500, {})], deps=["sr_rank_model"], model=S, expected=900)
    node_life(w, "SR", "sr_doc", "接口说明", 600, [
        ("dispatch", 5, {}), ("deliver", 120, {"outcome": "PASS", "summary": "说明已写"})],
        deps=["sr_rank_model"], model=F)
    w.add("milestone_claim", "router", ROUTERS["SR"], t(300), line="SR", milestone="M3",
          summary="留出集排序相关 0.84，超过 0.8", refs={"report": "docs/validation/router-SR/M3_CLAIM.md"})

    # ---- FE: M2 confirmed
    w.add("line_open", "coordinator", C2, t(3500), line="FE", summary="开 FE 主线")
    node_life(w, "FE", "fe_iface_draft", "评估器接口草案", 3400, [
        ("dispatch", 5, {}), ("deliver", 200, {"outcome": "PASS", "summary": "接口草案 v0"}),
        ("review", 260, {"outcome": "PARTIAL", "summary": "缺误差字段"})], model=O, milestones=["M2"])
    node_life(w, "FE", "fe_iface_v1", "接口 v1", 3000, [
        ("dispatch", 5, {}), ("deliver", 300, {"outcome": "PASS", "summary": "补上误差与置信字段"}),
        ("review", 360, {"outcome": "PASS", "summary": "双方签字"})], deps=["fe_iface_draft"], model=O,
        milestones=["M2"])
    w.add("milestone_claim", "router", ROUTERS["FE"], t(2600), line="FE", milestone="M2",
          summary="接口 v1 已签字", refs={"report": "docs/design/rsi/EVALUATOR_INTERFACE.md"})
    w.add("milestone_confirm", "coordinator", C2, t(2500), milestone="M2", summary="确认")
    node_life(w, "FE", "fe_search_lanes", "并行搜索通道", 1500, [
        ("dispatch", 10, {}), ("deliver", 700, {"outcome": "BLOCKED", "blocker": "design", "blocked_by": "通道合并策略未定",
                          "summary": "4 条通道跑通，合并策略未定"})],
        deps=["fe_iface_v1"], model=S, milestones=["M4"])
    node_life(w, "FE", "fe_merge_policy", "通道合并策略", 650, [
        ("dispatch", 10, {}), ("heartbeat", 300, {})], deps=["fe_search_lanes"], model=T, milestones=["M4"],
        expected=240)
    node_life(w, "FE", "fe_ui_probe", "可视化探针", 500, [
        ("dispatch", 5, {}), ("block", 200, {"blocker": "design", "summary": "设计没定输出格式"})],
        deps=["fe_iface_v1"], model=L)
    node_life(w, "FE", "fe_bench_e2e", "端到端基准", 200, [], deps=["fe_merge_policy"], model=S,
              milestones=["M4"])

    # ---- AG: struggling line, 2 blocked
    w.add("line_open", "coordinator", C2, t(2000), line="AG", summary="开 AG 主线")
    node_life(w, "AG", "ag_loop_skeleton", "决策回路骨架", 1900, [
        ("dispatch", 10, {}), ("deliver", 400, {"outcome": "PASS", "summary": "回路能跑一步"})], model=S,
        milestones=["M4"])
    node_life(w, "AG", "ag_reward", "奖励信号", 1400, [
        ("dispatch", 10, {}), ("block", 300, {"blocker": "dep", "summary": "等评估器多核结果"})],
        deps=["ag_loop_skeleton"], model=S, milestones=["M4"])
    node_life(w, "AG", "ag_memory", "长期记忆", 1200, [
        ("dispatch", 10, {}), ("block", 280, {"blocker": "env", "summary": "向量库服务连不上"})],
        deps=["ag_loop_skeleton"], model=L)
    node_life(w, "AG", "ag_policy_v0", "策略 v0", 1100, [
        ("dispatch", 10, {}), ("deliver", 300, {"outcome": "PASS", "result": "negative", "summary": "策略原地打转"})],
        deps=["ag_loop_skeleton"], model=T)
    node_life(w, "AG", "ag_policy_v1", "策略 v1", 700, [
        ("dispatch", 10, {})], deps=["ag_loop_skeleton"], model=S, milestones=["M4"], expected=300)
    w.add("node_supersede", "router", ROUTERS["AG"], t(690), line="AG", node="ag_policy_v0",
          by="ag_policy_v1", summary="改写策略后取代 v0")
    node_life(w, "AG", "ag_new_app", "新应用试跑", 300, [], deps=["ag_policy_v1", "ag_reward"], model=S,
              milestones=["M5"])

    # ---- KB
    w.add("line_open", "coordinator", C2, t(1500), line="KB", summary="开 KB 主线")
    node_life(w, "KB", "kb_paper_index", "论文索引", 1400, [
        ("dispatch", 5, {}), ("deliver", 200, {"outcome": "PASS", "summary": "320 篇入库"}),
        ("review", 260, {"outcome": "PASS", "summary": "抽查无误"})], model=F, milestones=["M5"])
    node_life(w, "KB", "kb_retrieval", "检索接口", 1100, [
        ("dispatch", 5, {}), ("deliver", 300, {"outcome": "PASS", "summary": "检索前 5 命中 0.9"})],
        deps=["kb_paper_index"], model=L)
    node_life(w, "KB", "kb_design_rules", "设计规则抽取", 600, [
        ("dispatch", 10, {})], deps=["kb_paper_index"], model=L, expected=600)
    node_life(w, "KB", "kb_rule_check", "规则核对", 200, [], deps=["kb_retrieval"], model=F)
    node_life(w, "KB", "kb_rule_mining", "规则挖掘试验", 900, [
        ("dispatch", 5, {}), ("deliver", 260, {"outcome": "PASS", "result": "negative", "summary": "抽出的规则大多不成立"})],
        deps=["kb_paper_index"], model=F)
    node_life(w, "KB", "kb_eval_set", "检索评测集", 820, [
        ("dispatch", 5, {}), ("deliver", 240, {"outcome": "PASS", "summary": "评测集 200 条"})],
        deps=["kb_paper_index"], model=L)
    node_life(w, "SR", "sr_stability", "稳定性复测", 450, [], deps=["sr_ablation"], model=F)
    node_life(w, "FE", "fe_cost_model", "搜索成本模型", 420, [], deps=["fe_iface_v1"], model=T)
    node_life(w, "KB", "kb_cache_layer", "检索缓存", 150, [
        ("cancel", 30, {"summary": "检索已够快，不做"})], model=F)

    # ---- ACC: imported line whose old graph had no status for most nodes (24 unknown beads)
    w.add("line_open", "import", "import", t(6000), line="ACC", summary="导入：混合评估器主线", source="import")
    for i in range(30):
        nid = "acc_part_%02d" % i
        w.add("node_plan", "import", "import", t(5900 - i), line="ACC", node=nid, title="ACC 子任务 %d" % i,
              summary="导入：旧图里的节点", source="import", status_unknown=i >= 6)
        if i < 6:
            w.add("node_dispatch", "import", "import", t(5800 - i), line="ACC", node=nid, summary="导入：已派",
                  source="import")
            w.add("node_deliver", "import", "import", t(5700 - i), line="ACC", node=nid, summary="导入：已交付",
                  outcome="PASS" if i % 3 else "PARTIAL", source="import")
    # DB is declared but has no nodes yet (empty lane)

    # ---- import burst: 15 corrections written in one go (actor import, not source import, so they show)
    for i in range(15):
        lid, nid = ("ACC", "acc_part_%02d" % i) if i < 6 else (
            ("BASE", ["base_gem5_rest", "base_report", "base_calib"][i % 3]) if i < 9 else
            ("EVB", ["evb_trace_ingest", "evb_cache_model", "evb_noc_model"][i % 3]))
        w.add("node_deliver", "import", "import", t(25 - i * 0.05), line=lid, node=nid,
              outcome="UNKNOWN" if i % 4 else "FAIL",
              summary="更正：orca 记录这个任务 11-02 1%d:%02d 已结束，不在跑；看板导入时误按旧记录标成在跑。" % (i % 10, i * 3))

    # ---- patrol
    w.add("nudge", "patrol", "patrol", t(180), line="AG", summary="AG 两个节点卡住超过 4 小时，请处理")
    w.add("auto_dispatch", "patrol", "patrol", t(95), line="KB", node="kb_rule_check",
          summary="依赖已满足，按规格代派", refs={"terminal": "term_wpatrol-kb", "worktree": "kb-kb_rule_check"},
          route={"model": F})
    w.add("observe", "patrol", "patrol", t(40), line="BASE", summary="BASE 的 router 终端不在，事件 16 小时没更新")
    w.add("nudge", "patrol", "patrol", t(12), line="SR", summary="M3 已报 5 小时，提醒核心一协调人确认")


SUB1 = "term_59c7976d-demo-evb-sub"      # EVB line -> sub-router
SUB2 = "term_6d46f485-demo-evb-leaf"     # sub-router -> sub-sub-router
SUB3 = "term_734f9d7b-demo-core2-audit"  # directly under core2's coordinator
SUB4 = "term_ee76d50a-demo-evb-leaf2"    # fourth level: below the sub-sub-router


def router_tree():
    def n(handle, kind, name, line, core_id, parent, edge, runs, present, out):
        return {"handle": handle, "kind": kind, "name": name, "line": line, "core": core_id, "parent": parent,
                "runs": runs, "present": present, "last_output_at": None if out is None else t(out), "edge": edge}
    tree = [n(C1, "core", "核心一 性能评估", None, "core1", None, "declared", ["run_c1"], True, 2),
            n(C2, "core", "核心二 Agent 闭环", None, "core2", None, "declared", ["run_c2"], True, 5)]
    for lid, name, core_id, out in (("EVB", "事件级基线评估器", "core1", 4), ("BASE", "基线数据与校准", "core1", 980),
                                    ("ACC", "混合评估器（ACC）：早先的核心一主体，近几天没有新节点", "core1", 300),
                                    ("DB", "评估数据集（DB）：带标签的设计和性能样本", "core1", 40),
                                    ("SR", "结构关系建模", "core1", 9), ("FE", "前端 Agent 搜索", "core2", 30),
                                    ("AG", "Agent 决策回路", "core2", 60), ("KB", "知识库与检索", "core2", 15)):
        tree.append(n(ROUTERS[lid], "line", name, lid, core_id, C1 if core_id == "core1" else C2, "default",
                      ["run_" + lid.lower()], lid != "BASE", out))
    tree += [n(SUB1, "sub", "EVB-多核扫描", "EVB", "core1", ROUTERS["EVB"], "dispatch", ["run_evb_sub"], True, 6),
             n(SUB2, "sub", "EVB-扫描-叶子（名字很长很长很长很长很长用来检查窄屏省略）", "EVB", "core1", SUB1, "worktree",
               ["run_evb_leaf"], True, 11),
             n(SUB4, "sub", "EVB-叶子-复核", "EVB", "core1", SUB2, "dispatch", ["run_evb_leaf2"], False, 70),
             n(SUB3, "sub", "核心二-巡检 router", None, "core2", C2, "dispatch", ["run_c2_audit"], True, 8)]
    return tree


def demo_live(stale=False):
    S, T, L, F, O = MODELS
    gen = t(45 if stale else 3)

    def rw(w, router):
        return dict(w, router=router, attribution="run")

    def wk(host, pid, model, started, line, node, wt):
        return {"host": host, "pid": pid, "workspace": "ws-%s::/home/x/%s" % (pid, wt), "worktree_name": wt,
                "terminal": "term_w%d" % pid, "model": model, "effort": "high", "started_at": t(started),
                "line": line, "node": node}
    return {
        "schema": "progress-live/v1", "generated_at": gen, "source": "demo-fixture",
        "hosts": {"mac": "ok", "wsl": "ok", "ib-agent": "unknown"},
        "router_tree": router_tree(),
        "routers": {
            "EVB": {"handle": ROUTERS["EVB"], "present": True, "last_output_at": t(4), "screen_flag": None},
            "BASE": {"handle": ROUTERS["BASE"], "present": False, "last_output_at": t(980), "screen_flag": None},
            "SR": {"handle": ROUTERS["SR"], "present": True, "last_output_at": t(9), "screen_flag": None},
            "FE": {"handle": ROUTERS["FE"], "present": True, "last_output_at": t(30), "screen_flag": "waiting_input"},
            "AG": {"handle": ROUTERS["AG"], "present": True, "last_output_at": t(60), "screen_flag": "stuck"},
            "KB": {"handle": ROUTERS["KB"], "present": True, "last_output_at": t(15), "screen_flag": None},
            "ACC": {"handle": ROUTERS["ACC"], "present": True, "last_output_at": t(300), "screen_flag": None},
            "DB": {"handle": ROUTERS["DB"], "present": True, "last_output_at": t(40), "screen_flag": None},
        },
        "workers": [
            wk("wsl", 2217, S, 870, "EVB", "evb_multi_core", "evb-evb_multi_core"),
            wk("wsl", 2301, S, 780, "SR", "sr_transfer", "sr-sr_transfer"),
            wk("wsl", 2444, T, 640, "FE", "fe_merge_policy", "fe-fe_merge_policy"),
            wk("mac", 811, S, 690, "AG", "ag_policy_v1", "ag-ag_policy_v1"),
            wk("wsl", 2590, L, 590, "KB", "kb_design_rules", "kb-kb_design_rules"),
            wk("wsl", 2602, F, 95, "KB", "kb_rule_check", "kb-kb_rule_check"),
            # core1's coordinator dispatches two workers of its own (no line)
            dict(wk("mac", 905, O, 50, None, None, "core1-review-1104"), router=C1, core="core1", attribution="run"),
            dict(wk("wsl", 2950, T, 130, None, None, "core1-bd-r1-review"), router=C1, core="core1",
                 attribution="run"),
            # lingering: the orca task finished 30 minutes ago but the process is still alive
            dict(wk("wsl", 2960, T, 700, "EVB", "evb_noc_model", "evb-evb_noc_model"), lingering=True,
                 task={"id": "task_demo_noc", "run": "run_evb", "dispatch": "ctx_noc", "state": "succeeded",
                       "dispatched_at": t(700), "completed_at": t(30)}),
            # stalled: the screen shows an error and nothing has moved for 47 minutes
            dict(wk("wsl", 2970, O, 280, "EVB", "evb_calib_review", "evb-evb_calib_review"),
                 screen={"flag": "error", "line": "Codex: Bad Request (invalid_request_error)",
                         "checked_at": gen, "since": t(47)}),
            wk("wsl", 2710, L, 240, None, None, "scratch-bench-probe"),
            wk("mac", 930, T, 20, "EVB", None, "evb-hotfix"),
            wk("wsl", 2799, F, 8, None, None, "kb-reindex-tmp"),
            # router tree: workers dispatched by routers below the line level
            rw(wk("mac", 940, O, 35, "EVB", None, "evb-sub-probe"), SUB1),
            rw(wk("wsl", 2811, T, 70, "EVB", None, "evb-sub-sweep"), SUB1),
            rw(wk("wsl", 2820, L, 25, "EVB", None, "evb-leaf-a"), SUB2),
            rw(wk("wsl", 2821, F, 12, "EVB", None, "evb-leaf-b"), SUB2),
            rw(wk("mac", 950, S, 45, None, None, "core2-audit-1104"), SUB3),
            rw(wk("wsl", 2830, L, 18, None, None, "core2-audit-sweep"), SUB3),
            rw(wk("mac", 960, O, 9, "EVB", None, "evb-leaf2-check"), SUB4),
        ],
        "errors": ["ib-agent: ssh 超时，本次未采集"],
    }


def paper_doc():
    return {
        "schema": core.SCHEMA_PROJECT, "id": "paper", "name": "DAC 论文写作",
        "final_goal": {"text": "按时投出 DAC 论文", "done_when": "系统提交成功"},
        "cores": [{"id": "w", "name": "写作", "coordinator": C1}],
        "lines": [{"id": "DRAFT", "name": "正文", "core": "w", "router": ROUTERS["EVB"]}],
        "milestones": [
            {"id": "P1", "title": "大纲定稿", "core": "w", "lines": ["DRAFT"], "after": []},
            {"id": "P2", "title": "实验图齐", "core": "w", "lines": ["DRAFT"], "after": ["P1"]},
            {"id": "P3", "title": "全文初稿", "core": "w", "lines": ["DRAFT"], "after": ["P2"]},
        ],
    }


def build_paper(w):
    r = ROUTERS["EVB"]
    w.add("node_plan", "router", r, t(800), line="DRAFT", node="outline", title="大纲", summary="排好大纲")
    w.add("node_dispatch", "router", r, t(790), line="DRAFT", node="outline", summary="派出",
          refs={"terminal": "term_wpaper"})
    w.add("node_deliver", "worker", "term_wpaper", t(500), line="DRAFT", node="outline", outcome="PASS",
          summary="大纲交付")
    w.add("milestone_claim", "router", r, t(480), line="DRAFT", milestone="P1", summary="大纲完成")
    w.add("milestone_confirm", "coordinator", C1, t(400), milestone="P1", summary="确认")
    w.add("node_plan", "router", r, t(300), line="DRAFT", node="figs", title="实验图", deps=["outline"],
          summary="排好实验图")


def demo_supervisor():
    return {
        "schema": "progress-supervisor/v1", "generated_at": t(8), "model": "gpt-6.1-terra",
        "duration_s": 312, "inspect_calls": 23,
        "summary": "整体在推进。EVB 的执行者连续两轮重跑同一个校准脚本、没有新产出，需要 router 介入；"
                   "SR 有两个可以并行的节点还在排队。其余主线正常。",
        "lines": [
            {"line": "EVB", "status": "problem", "progress_note": "校准节点 40 分钟没有新交付",
             "issues": [{"kind": "looping", "evidence": "屏幕里连续两次执行 calibrate.py，输出相同"}],
             "suggestions": ["让执行者先贴出第一次的报错再重跑", "必要时换一个执行者接手"]},
            {"line": "SR", "status": "attention", "progress_note": "两个独立节点在串行等待",
             "issues": [{"kind": "idle_parallelism", "evidence": "有空闲执行者，但节点 sr-3 与 sr-4 没有依赖却未同时派出"}],
             "suggestions": ["同时派出 sr-3 与 sr-4"]},
            {"line": "BASE", "status": "ok", "progress_note": "正常", "issues": [], "suggestions": []},
            {"line": "FE", "status": "ok", "progress_note": "正常", "issues": [], "suggestions": []},
            {"line": "AG", "status": "ok", "progress_note": "正常", "issues": [], "suggestions": []},
        ],
        "delivery": [
            {"line": "EVB", "target": "term_evb", "sent": True, "reason": "sent"},
            {"line": "SR", "target": "term_sr", "sent": False, "reason": "rate_limited"},
        ],
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home", help="store directory (default $PROGRESS_HOME)")
    p.add_argument("--stale-live", action="store_true", help="write a live.json older than 30 minutes")
    p.add_argument("--no-paper", action="store_true", help="only the demo project")
    a = p.parse_args(argv)
    if a.home:
        os.environ["PROGRESS_HOME"] = a.home
    if not os.environ.get("PROGRESS_HOME"):
        p.error("set --home or PROGRESS_HOME; refusing to write into ~/.progress")
    root = core.home()
    for pid in ("demo", "paper"):
        if (root / pid).exists():
            shutil.rmtree(root / pid)
    core.save_project(project_doc())
    w = Writer("demo")
    build_demo(w)
    core.save_live("demo", demo_live(a.stale_live))
    core.save_supervisor("demo", demo_supervisor())
    n = w.n
    if not a.no_paper:
        core.save_project(paper_doc())
        wp = Writer("paper")
        build_paper(wp)
        n += wp.n
    st = core.fold(core.load_project("demo"), core.read_events("demo"))
    nodes = sum(len(l["nodes"]) for l in st["lines"].values())
    counts = {}
    for l in st["lines"].values():
        for k, v in l["counts"].items():
            counts[k] = counts.get(k, 0) + v
    print("wrote %d events into %s; demo nodes=%d %s" % (n, root, nodes, counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
