"""Decide panel (FRAMEWORK §5 tier 2) and the QQ two-way loop, driven by patrol under --apply.

- A ready `decide` node is judged by Claude Opus and GPT Astra independently (two subprocesses in parallel).
  Both valid, same choice, neither asks for a human -> AGREED: `decision` event, `node_deliver` (patrol,
  decide nodes only, refs.card), QQ notice. Anything else -> ESCALATED: `decision` (escalated|panel_failed),
  `node_block`, QQ decision card.
- Replies the Hermes plugin `progress-qq` put in ~/.progress/inbox.jsonl are applied here; every applied entry
  writes a `human_reply` event (its refs.message_id is the "applied" marker).
- Human actor for a pick: role human with handle qq:<owner> when project.humans lists it (validation stays
  strict); otherwise the event is written as patrol with refs.by = qq:<owner>.
- Env overrides (tests): PROGRESS_DECIDE_OPUS_CMD / PROGRESS_DECIDE_ASTRA_CMD (shlex, prompt on stdin, stdout =
  JSON verdict), HERMES_BIN (QQ sender, default ~/.local/bin/hermes).
- Mac-local files under $PROGRESS_HOME (~/.progress): qq.json {"target","owner"}, inbox.jsonl, qq_cards.json
  {card: {project,line,node,status,options}}, decide.lock.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

import progress_core as core

PER_ROUND = 2
TIMEOUT_S = 480
TRIES = 2
QQ_MAX = 1500
HERMES_DEFAULT = os.path.expanduser("~/.local/bin/hermes")
CONFIDENCE = ("low", "medium", "high")

VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["choice", "confidence", "needs_human", "human_reason", "reasons", "would_change_if",
                 "plain_summary"],
    "properties": {
        "choice": {"type": "integer", "description": "1-based option number"},
        "confidence": {"type": "string", "enum": list(CONFIDENCE)},
        "needs_human": {"type": "boolean"},
        "human_reason": {"type": "string"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "would_change_if": {"type": "string"},
        "plain_summary": {"type": "string"},
    },
}
WHY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["explanation"],
              "properties": {"explanation": {"type": "string"}}}


def _short(text: Any, n: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _fit(text: str, n: int = QQ_MAX) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


# ------------------------------------------------------------------ files

def qq_path() -> pathlib.Path:
    return core.home() / "qq.json"


def inbox_path() -> pathlib.Path:
    return core.home() / "inbox.jsonl"


def cards_path() -> pathlib.Path:
    return core.home() / "qq_cards.json"


def load_qq() -> Optional[Dict[str, str]]:
    try:
        doc = json.loads(qq_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not doc.get("target"):
        return None
    return {"target": str(doc["target"]), "owner": str(doc.get("owner") or "")}


def load_cards() -> Dict[str, Any]:
    try:
        doc = json.loads(cards_path().read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save_card(card: str, info: Dict[str, Any]) -> None:
    cards = load_cards()
    cards[card] = info
    p = cards_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(cards, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(str(tmp), str(p))


def read_inbox() -> List[Dict[str, Any]]:
    out = []
    try:
        with inbox_path().open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(e, dict):
                    out.append(e)
    except OSError:
        pass
    return out


def entry_id(e: Dict[str, Any]) -> str:
    mid = e.get("message_id")
    if mid:
        return str(mid)
    return "h_" + hashlib.sha1(("%s|%s|%s" % (e.get("at"), e.get("card"), e.get("text"))).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ events helpers

def card_no(card: Any) -> int:
    m = re.match(r"^D(\d+)$", str(card or ""))
    return int(m.group(1)) if m else 0


def next_card(events: List[Dict[str, Any]]) -> str:
    return "D%d" % (1 + max([card_no(e.get("card")) for e in events if e.get("kind") == "decision"] or [0]))


def has_decision(events: List[Dict[str, Any]], line: str, node: str, spec: Optional[str]) -> bool:
    return any(e.get("kind") == "decision" and e.get("line") == line and e.get("node") == node
               and (e.get("refs") or {}).get("spec") == spec for e in events)


def latest_decision(events: List[Dict[str, Any]], card: str) -> Optional[Dict[str, Any]]:
    found = [e for e in events if e.get("kind") == "decision" and e.get("card") == card]
    return found[-1] if found else None


def applied_ids(events: List[Dict[str, Any]]) -> set:
    return {(e.get("refs") or {}).get("message_id") for e in events if e.get("kind") == "human_reply"}


def pending_inbox(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Inbox entries for cards of this project that no human_reply event covers yet."""
    cards = {e.get("card") for e in events if e.get("kind") == "decision"}
    done = applied_ids(events)
    return [e for e in read_inbox() if e.get("card") in cards and entry_id(e) not in done]


# ------------------------------------------------------------------ spec parsing

_OPT = re.compile(r"^\s*(?:[-*]\s*)?(?:选项|方案|Option\s*)?\s*(\d{1,2})\s*[\.、)）:：]\s*(\S.*)$")


def parse_options(text: str) -> List[str]:
    """First run of consecutive numbered items starting at 1 (length >= 2), else []."""
    runs: List[List[str]] = []
    cur: List[str] = []
    for ln in (text or "").splitlines():
        m = _OPT.match(ln)
        if not m:
            continue
        k = int(m.group(1))
        if k == 1:
            cur = [m.group(2).strip()]
            runs.append(cur)
        elif cur and k == len(cur) + 1:
            cur.append(m.group(2).strip())
    for r in runs:
        if len(r) >= 2:
            return r
    return []


def question_of(text: str, title: str = "") -> str:
    for ln in (text or "").splitlines():
        m = re.match(r"^\s*(?:#+\s*)?(?:问题|Question)\s*[:：]\s*(\S.*)$", ln)
        if m:
            return _short(m.group(1), 80)
    for ln in (text or "").splitlines():
        s = ln.strip().lstrip("#").strip()
        if s and not s.startswith("{") and not s.startswith("<!--"):
            return _short(s, 80)
    return _short(title, 80)


def build_case(project: Dict[str, Any], lid: str, nid: str, nodes: Dict[str, Any], spec_text: str) -> Dict[str, Any]:
    n = nodes[nid]
    parent = nodes.get(n.get("parent") or "") or {}
    deps = []
    for d in (n.get("deps") or [])[:4]:
        dn = nodes.get(d) or {}
        dels = [h for h in dn.get("history") or [] if h.get("kind") == "node_deliver"]
        deps.append({"node": d, "summary": (dels[-1].get("summary") if dels else dn.get("summary")),
                     "outcome": dn.get("outcome"), "refs": dn.get("refs") or {}})
    options = parse_options(spec_text)
    return {"line": lid, "node": nid, "title": n.get("title") or nid, "spec": (spec_text or "")[:12000],
            "options": options, "question": question_of(spec_text, n.get("title") or nid),
            "parent_summary": parent.get("summary") or parent.get("title") or "",
            "final_goal": ((project.get("final_goal") or {}).get("text") or ""), "deps": deps}


def case_text(c: Dict[str, Any]) -> str:
    deps = "\n".join("- %s（%s）：%s %s" % (d["node"], d.get("outcome") or "?", d.get("summary") or "",
                                          json.dumps(d.get("refs") or {}, ensure_ascii=False)) for d in c["deps"]) or "（无）"
    opts = "\n".join("%d. %s" % (i + 1, o) for i, o in enumerate(c["options"]))
    return ("主线 %s，节点 %s（%s）\n最终目标：%s\n上级计划：%s\n依赖节点最近的交付：\n%s\n\n选项：\n%s\n\n节点 spec 全文：\n%s\n"
            % (c["line"], c["node"], c["title"], c["final_goal"] or "（未写）", c["parent_summary"] or "（无）", deps, opts,
               c["spec"]))


def judge_prompt(c: Dict[str, Any]) -> str:
    return (
        "任务：对下面这个技术裁决独立判断，只能从列出的选项里选一个。可以用 Read/Grep/Glob 只读查看当前仓库来核对证据，不要修改任何东西。\n\n"
        "失效清单（逐项检查，发现就写进 reasons）：\n"
        "- 数字没有出处\n- 对照或基线没有重跑\n- 数据泄漏（测试集信息进了训练或选择）\n- 评估代码被改动\n"
        "- 结论强于证据（效应量、样本量不够）\n- 只报告了好结果\n\n"
        "程序：1. 列出这个选择依赖的假设。2. 逐条对照 spec 引用的证据核对，reasons 里每条都写明引用的文件或日志位置。"
        "3. 推演这个选择会怎样失败。4. 写出什么证据会推翻你的选择（would_change_if）。\n\n"
        "立场：默认对抗。没把握就选更保守的选项，并在 reasons 里说明。\n\n"
        "升级判断：needs_human 只在命中下面之一时为 true，并在 human_reason 写明是哪一条：目标与价值（改最终目标、成功标准、范围，"
        "伦理或安全）；git 撤不回的代价（超预算花钱或算力、对外发布或调用外部服务、删除版本库之外的数据、动共享资源）；"
        "留出集（读取最终测试集）；只有人有的真值（实验室数据、领域访问权限、“这是不是你要的”）；反复失败。"
        "其他技术取舍自己判断，不要上交；此时 human_reason 留空。\n\n"
        "输出：只输出符合 schema 的 JSON，所有文字字段（reasons、would_change_if、plain_summary、human_reason）一律用中文。choice 是选项编号（从 1 开始）。plain_summary 不超过 120 字，白话中文，术语当场解释。\n\n"
        "=== 案例 ===\n" + case_text(c))


def why_prompt(c: Dict[str, Any], verdicts: Dict[str, Any], result: str, choice: Any, ask: str) -> str:
    return ("任务：用户对下面这个已作出的技术裁决有疑问，请用白话中文解释。解释不超过 600 字，术语当场用一句话讲明白，"
            "先说结论，再说为什么、另一个方案为什么没选、什么情况下结论会变。可以用 Read/Grep/Glob 只读核对证据，不要修改任何东西。\n"
            "输出：只输出 JSON {\"explanation\": \"...\"}。\n\n裁决结果：%s，选 %s\n两位评审的意见：\n%s\n\n用户的问题：%s\n\n=== 案例 ===\n%s"
            % (result, choice, json.dumps(verdicts, ensure_ascii=False, indent=1), ask or "为什么这么选", case_text(c)))


# ------------------------------------------------------------------ model calls

def _loads_lenient(text: str) -> Any:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    return json.loads(text)


def extract_json(stdout: str) -> Optional[Dict[str, Any]]:
    """The CLI's JSON result: structured_output if present, else `result` text as JSON, else the object itself."""
    try:
        doc = _loads_lenient(stdout)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    if isinstance(doc.get("structured_output"), dict):
        return doc["structured_output"]
    if isinstance(doc.get("result"), str):
        try:
            inner = _loads_lenient(doc["result"])
            if isinstance(inner, dict):
                return inner
        except ValueError:
            pass
    return doc


def _run(argv: List[str], prompt: str, cwd: Optional[str], timeout: int) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=timeout,
                           cwd=cwd if cwd and os.path.isdir(cwd) else None)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %ss" % timeout
    except OSError as exc:
        return 127, "", str(exc)


def call_opus(prompt: str, cwd: Optional[str], schema: Dict[str, Any], timeout: int = TIMEOUT_S) -> Optional[Dict[str, Any]]:
    custom = os.environ.get("PROGRESS_DECIDE_OPUS_CMD")
    argv = shlex.split(custom) if custom else [
        "claude", "-p", "--model", "opus", "--output-format", "json", "--json-schema",
        json.dumps(schema, ensure_ascii=False), "--allowedTools", "Read", "Grep", "Glob"]
    rc, out, _err = _run(argv, prompt, cwd, timeout)
    return extract_json(out) if rc == 0 else None


def call_astra(prompt: str, cwd: Optional[str], schema: Dict[str, Any], timeout: int = TIMEOUT_S) -> Optional[Dict[str, Any]]:
    custom = os.environ.get("PROGRESS_DECIDE_ASTRA_CMD")
    if custom:
        rc, out, _err = _run(shlex.split(custom), prompt, cwd, timeout)
        return extract_json(out) if rc == 0 else None
    with tempfile.TemporaryDirectory(prefix="decide-") as td:
        sp, op = os.path.join(td, "schema.json"), os.path.join(td, "out.json")
        pathlib.Path(sp).write_text(json.dumps(schema), encoding="utf-8")
        argv = ["codex", "exec", "-m", "gpt-6-astra", "-c", 'model_reasoning_effort="high"', "--sandbox", "read-only",
                "--skip-git-repo-check", "--ephemeral", "--output-schema", sp, "-o", op, "-"]
        rc, _out, _err = _run(argv, prompt, cwd, timeout)
        if rc != 0:
            return None
        try:
            return extract_json(pathlib.Path(op).read_text(encoding="utf-8"))
        except OSError:
            return None


def valid_verdict(v: Any, n_options: int) -> Optional[Dict[str, Any]]:
    if not isinstance(v, dict):
        return None
    ch = v.get("choice")
    if isinstance(ch, bool) or not isinstance(ch, int) or not 1 <= ch <= n_options:
        return None
    if v.get("confidence") not in CONFIDENCE or not isinstance(v.get("needs_human"), bool):
        return None
    reasons = v.get("reasons")
    if not isinstance(reasons, list) or not [r for r in reasons if isinstance(r, str) and r.strip()]:
        return None
    if not isinstance(v.get("plain_summary"), str) or not v["plain_summary"].strip():
        return None
    return {"choice": ch, "confidence": v["confidence"], "needs_human": v["needs_human"],
            "human_reason": _short(v.get("human_reason") or "", 80),
            "reasons": [_short(r, 160) for r in reasons if isinstance(r, str) and r.strip()][:4],
            "would_change_if": _short(v.get("would_change_if") or "", 160),
            "plain_summary": _short(v["plain_summary"], 120)}


def run_panel(c: Dict[str, Any], cwd: Optional[str]) -> Dict[str, Optional[Dict[str, Any]]]:
    """Both judges in parallel threads, each tried up to TRIES times; a failed judge is None."""
    prompt = judge_prompt(c)
    n = len(c["options"])
    res: Dict[str, Optional[Dict[str, Any]]] = {"opus": None, "astra": None}

    def go(name, fn):
        for _ in range(TRIES):
            try:
                v = valid_verdict(fn(prompt, cwd, VERDICT_SCHEMA), n)
            except Exception:  # a judge must never take patrol down
                v = None
            if v:
                res[name] = v
                return

    ts = [threading.Thread(target=go, args=("opus", call_opus)), threading.Thread(target=go, args=("astra", call_astra))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return res


def run_explain(prompt: str, cwd: Optional[str]) -> Optional[str]:
    for _ in range(TRIES):
        try:
            doc = call_opus(prompt, cwd, WHY_SCHEMA)
        except Exception:
            doc = None
        if isinstance(doc, dict) and isinstance(doc.get("explanation"), str) and doc["explanation"].strip():
            return doc["explanation"].strip()[:600]
    return None


# ------------------------------------------------------------------ QQ

def send_qq(runner, qq: Optional[Dict[str, str]], text: str) -> Tuple[bool, str]:
    if not qq:
        return False, "qq_unconfigured"
    hb = os.environ.get("HERMES_BIN") or HERMES_DEFAULT
    rc, out, err = runner([hb, "send", "-t", qq["target"], "-q", _fit(text)], timeout=60)
    return rc == 0, (err or out or "")[:200] if rc != 0 else ""


def agreed_notice(card: str, c: Dict[str, Any], choice: int, vs: Dict[str, Any]) -> str:
    return _fit("[已执行 %s] %s/%s：%s\n结论：选 %d %s\n理由：%s / %s\n推翻：回复「%s 推翻」；要解释：回复「%s 为什么」" % (
        card, c["line"], c["node"], c["question"], choice, _short(c["options"][choice - 1], 200),
        vs["opus"]["plain_summary"], vs["astra"]["plain_summary"], card, card))


def _opinion(name: str, v: Optional[Dict[str, Any]], opts: List[str]) -> str:
    if not v:
        return "%s：没有给出有效意见（两次都失败）" % name
    head = "%s：选 %d %s（把握 %s）" % (name, v["choice"], _short(opts[v["choice"] - 1], 60), v["confidence"])
    if v["needs_human"]:
        head += "；认为需要人定：" + (v["human_reason"] or "未说明")
    return head + "\n  " + "\n  ".join("- " + r for r in v["reasons"][:3])


def card_messages(card: str, c: Dict[str, Any], vs: Dict[str, Any], result: str) -> List[str]:
    opts = c["options"]
    first = ("[待你裁决 %s] %s/%s\n要定什么：%s\n为什么现在定：%s 线的节点 %s 卡在这个裁决上；其余节点不受影响。\n"
             "代价：各选项的代价见下，选了就按它执行\n白话背景：本项目目标是「%s」；这个节点要在下面方案里定一个。\n\n选项：\n%s\n\n"
             "先说你的直觉选哪个，再看下面 AI 的意见。") % (
        card, c["line"], c["node"], c["question"], c["line"], c["node"], _short(c["final_goal"] or "未写", 100),
        "\n".join("%d. %s" % (i + 1, _short(o, 200)) for i, o in enumerate(opts)))
    o, a = vs.get("opus"), vs.get("astra")
    if result == "panel_failed":
        why = "面板没跑完：" + "、".join(n for n, v in (("Opus", o), ("Astra", a)) if not v) + " 两次都没给出有效意见"
    elif o and a and o["choice"] != a["choice"]:
        why = "分歧点：Opus 选 %d，Astra 选 %d" % (o["choice"], a["choice"])
    else:
        why = "分歧点：评审认为这件事需要人定（见上）"
    flips = "；".join("%s：%s" % (n, v["would_change_if"]) for n, v in (("Opus", o), ("Astra", a)) if v and v["would_change_if"])
    second = ("[%s 续] AI 意见（请先想好你的直觉）\n%s\n%s\n%s\n什么证据会改变结论：%s\n不回复会怎样：该节点保持暂停，其他工作照常推进。\n"
              "回复格式：「%s 选 2」或「%s 为什么」") % (
        card, _opinion("Opus", o, opts), _opinion("Astra", a, opts), why, flips or "两方都没写", card, card)
    return [_fit(first), _fit(second)]


# ------------------------------------------------------------------ the round

class Ctx:
    """Everything a round needs from patrol (no circular import): store, runner, writers, hooks."""

    def __init__(self, store, runner, write, read_spec, repo_path, send_router, budget):
        self.store, self.runner, self.write = store, runner, write
        self.read_spec, self.repo_path, self.send_router, self.budget = read_spec, repo_path, send_router, budget
        self.project, self.events, self.live = store.read()
        self.qq = load_qq()
        self.done: List[Dict[str, Any]] = []

    def state(self):
        return core.fold(self.project, self.events)

    def W(self, ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            res = self.write(ev)
        except core.ProgressError as exc:
            self.done.append({"type": "decide_event_error", "kind": ev.get("kind"), "node": ev.get("node"),
                              "error": exc.as_dict()})
            return None
        try:
            self.project, self.events, self.live = self.store.read()  # fold needs the stored timestamps
        except core.ProgressError:
            self.events.append(dict(ev, id=res.get("id")))
        return res

    def say(self, line: str, text: str, key: str) -> bool:
        ok, why = send_qq(self.runner, self.qq, text)
        if not ok:
            self.done.append({"type": "qq_send", "sent": False, "error": why})
            self.W({"kind": "observe", "line": line, "summary": _short("QQ 发送失败：%s" % why),
                    "refs": {"key": "qq_send_fail:%s" % key}})
        return ok

    def line_node(self, lid: str, nid: str):
        line = core.line_of(self.project, lid)
        return line, ((self.state()["lines"].get(lid) or {}).get("nodes") or {}).get(nid)

    def case_for(self, lid: str, nid: str, spec_ref: Optional[str]):
        line, n = self.line_node(lid, nid)
        if not line or not n:
            return None, None, "节点不存在"
        repo = self.repo_path(self.project, line)
        spec = self.read_spec(spec_ref or n.get("spec"), repo, self.runner)
        if spec.get("error"):
            return None, repo, spec["error"]
        return build_case(self.project, lid, nid, self.state()["lines"][lid]["nodes"], spec["text"]), repo, None


def _write_cards(ctx: Ctx, card: str, c: Dict[str, Any], status: str, **extra) -> None:
    try:
        save_card(card, dict({"project": ctx.store.pid, "line": c["line"], "node": c["node"], "status": status,
                              "options": c["options"]}, **extra))
    except OSError as exc:
        ctx.done.append({"type": "cards_file_error", "error": str(exc)})


def judge_one(ctx: Ctx, act: Dict[str, Any]) -> None:
    lid, nid = act["line"], act["node"]
    nodes = (ctx.state()["lines"].get(lid) or {}).get("nodes") or {}
    n = nodes.get(nid)
    if not n or n.get("status") != "ready" or has_decision(ctx.events, lid, nid, n.get("spec")):
        ctx.done.append({"type": "decide", "node": nid, "skipped": "not_ready_or_decided"})
        return
    c, repo, err = ctx.case_for(lid, nid, n.get("spec"))
    if err or len(c["options"]) < 2:
        ctx.done.append({"type": "decide", "node": nid, "skipped": err or "no_options"})
        return
    ctx.budget[0] -= 1
    vs = run_panel(c, repo)
    o, a = vs["opus"], vs["astra"]
    card = next_card(ctx.events)
    spec_ref = n.get("spec")
    agreed = bool(o and a and o["choice"] == a["choice"] and not o["needs_human"] and not a["needs_human"])
    result = "agreed" if agreed else ("panel_failed" if not (o and a) else "escalated")
    choice = o["choice"] if agreed else None
    ev = {"kind": "decision", "line": lid, "node": nid, "card": card, "result": result, "choice": choice,
          "option": c["options"][choice - 1][:200] if choice else None, "verdicts": vs,
          "options": [x[:200] for x in c["options"]], "refs": {"card": card, "spec": spec_ref},
          "summary": _short("面板一致 %s：选 %s" % (card, choice) if agreed else
                            "面板未一致 %s（%s），交用户裁决" % (card, result))}
    if not ctx.W(ev):
        return
    rec = {"type": "decide", "node": nid, "card": card, "result": result}
    if agreed:
        done_ev = ctx.W({"kind": "node_deliver", "line": lid, "node": nid, "outcome": "PASS",
                         "summary": _short("面板一致：选 %d %s" % (choice, c["options"][choice - 1])),
                         "refs": {"card": card, "spec": spec_ref}})
        if done_ev:
            _write_cards(ctx, card, c, "agreed", choice=choice)
            rec["sent"] = ctx.say(lid, agreed_notice(card, c, choice, vs), "agreed:%s" % card) if ctx.qq else False
        else:
            ctx.W({"kind": "observe", "line": lid, "node": nid, "summary": _short("%s 已裁决但交付没写进去" % card),
                   "refs": {"key": "decide_deliver:%s" % card}})
    else:
        ctx.W({"kind": "node_block", "line": lid, "node": nid, "blocker": "design",
               "summary": "等用户裁决 %s" % card})
        _write_cards(ctx, card, c, "escalated")
        if ctx.qq:
            rec["sent"] = all([ctx.say(lid, m, "card:%s" % card) for m in card_messages(card, c, vs, result)])
        else:
            line = core.line_of(ctx.project, lid) or {}
            target = (((ctx.live or {}).get("routers") or {}).get(lid) or {}).get("handle") or line.get("router")
            if target:
                ctx.send_router(target, "[巡检] %s 需要用户裁决（%s/%s：%s），但 QQ 未配置，请把这个决定转告用户。" % (
                    card, lid, nid, c["question"]))
    ctx.done.append(rec)


# ------------------------------------------------------------------ inbox

def human_actor(project: Dict[str, Any], owner: str) -> Tuple[Optional[Dict[str, str]], Dict[str, str]]:
    """(actor, extra refs): human role when project.humans lists qq:<owner>, else patrol + refs.by."""
    h = "qq:%s" % owner
    if owner and any(core._same(h, x) for x in core.human_handles(project)):
        return {"role": "human", "handle": h}, {}
    return None, {"by": h}


def _reply_event(ctx, lid, nid, e, outcome, text) -> None:
    ctx.W({"kind": "human_reply", "line": lid, "node": nid, "card": e.get("card"),
           "summary": _short("用户回复 %s：%s（%s）" % (e.get("card"), text, outcome)),
           "refs": {"message_id": entry_id(e), "card": e.get("card"), "verb": e.get("verb"), "outcome": outcome}})


def apply_inbox(ctx: Ctx) -> None:
    owner = (ctx.qq or {}).get("owner") or ""
    seen = set()
    for e in pending_inbox(ctx.events):
        if entry_id(e) in seen:
            continue  # the same message twice in one inbox
        seen.add(entry_id(e))
        card, verb = e.get("card"), e.get("verb")
        dec = latest_decision(ctx.events, card)
        if not dec:
            continue
        lid, nid = dec["line"], dec["node"]
        opts = dec.get("options") or []
        res = dec.get("result")
        reply = None  # (ack text)
        if verb == "why":
            if ctx.budget[0] <= 0:
                continue  # next round
            ctx.budget[0] -= 1
            c, repo, err = ctx.case_for(lid, nid, (dec.get("refs") or {}).get("spec"))
            text = None
            if c:
                c["options"] = c["options"] or opts
                text = run_explain(why_prompt(c, dec.get("verdicts") or {}, res, dec.get("choice"), str(e.get("arg") or "")), repo)
            reply = "[%s 解释]\n%s" % (card, text) if text else "%s 的解释这次没生成出来（%s），请稍后再问一次。" % (card, err or "模型没有给出有效回答")
            _reply_event(ctx, lid, nid, e, "explained" if text else "explain_failed", str(e.get("arg") or "为什么"))
            ctx.say(lid, reply, "why:%s" % entry_id(e))
            ctx.done.append({"type": "human_reply", "card": card, "verb": verb, "applied": bool(text)})
            continue
        if verb == "pick":
            try:
                k = int(e.get("arg"))
            except (TypeError, ValueError):
                k = 0
            if not 1 <= k <= len(opts):
                outcome, reply = "invalid_choice", "%s 没有选项 %s（共 %d 个），没有改动。" % (card, e.get("arg"), len(opts))
            elif res in ("escalated", "panel_failed"):
                outcome, reply = _human_pick(ctx, dec, e, k, owner)
            elif res == "agreed" and k != dec.get("choice"):
                outcome, reply = _overrule(ctx, dec, e, "改为 选 %d %s" % (k, opts[k - 1]), k)
            elif res == "agreed":
                outcome, reply = "same_choice", "%s 本来就是选 %d，无需改动。" % (card, k)
            else:
                outcome, reply = "already_handled", "%s 已经处理过（%s），没有改动。" % (card, res)
        else:  # overrule
            if res == "agreed":
                arg = str(e.get("arg") or "").strip()
                outcome, reply = _overrule(ctx, dec, e, "推翻%s" % ("，理由：" + arg if arg else "，没给理由，请先向用户确认改成什么"), None)
            elif res in ("escalated", "panel_failed"):
                outcome, reply = "not_decided", "%s 还没有结论，请回复「%s 选 k」。" % (card, card)
            else:
                outcome, reply = "already_handled", "%s 已经处理过（%s），没有改动。" % (card, res)
        _reply_event(ctx, lid, nid, e, outcome, str(e.get("text") or "")[:40])
        ctx.say(lid, reply, "ack:%s" % entry_id(e))
        ctx.done.append({"type": "human_reply", "card": card, "verb": verb, "outcome": outcome})


def _human_pick(ctx: Ctx, dec: Dict[str, Any], e: Dict[str, Any], k: int, owner: str):
    lid, nid, card = dec["line"], dec["node"], dec["card"]
    opt = (dec.get("options") or [""] * k)[k - 1]
    actor, extra = human_actor(ctx.project, owner)
    base = dict({"actor": actor} if actor else {})
    refs = dict({"card": card, "spec": (dec.get("refs") or {}).get("spec")}, **extra)
    ok = ctx.W(dict(base, kind="decision", line=lid, node=nid, card=card, result="human", choice=k, option=opt[:200],
                    refs=refs, summary=_short("用户裁决 %s：选 %d" % (card, k))))
    if not ok:
        return "write_failed", "%s 没写进去，请稍后再回复一次。" % card
    ctx.W(dict(base, kind="node_deliver", line=lid, node=nid, outcome="PASS", refs=refs,
               summary=_short("用户裁决：选 %d %s" % (k, opt))))
    info = load_cards().get(card) or {"project": ctx.store.pid, "line": lid, "node": nid, "options": dec.get("options")}
    try:
        save_card(card, dict(info, status="human", choice=k))
    except OSError:
        pass
    return "applied", "%s 已生效：选 %d" % (card, k)


def _overrule(ctx: Ctx, dec: Dict[str, Any], e: Dict[str, Any], what: str, k: Optional[int]):
    lid, nid, card = dec["line"], dec["node"], dec["card"]
    opts = dec.get("options") or []
    actor, extra = human_actor(ctx.project, (ctx.qq or {}).get("owner") or "")
    refs = dict({"card": card, "spec": (dec.get("refs") or {}).get("spec")}, **extra)
    ok = ctx.W(dict({"actor": actor} if actor else {}, kind="decision", line=lid, node=nid, card=card, result="overruled",
                    choice=k, option=(opts[k - 1][:200] if k else None), refs=refs,
                    summary=_short("用户推翻 %s：%s" % (card, what))))
    if not ok:
        return "write_failed", "%s 没写进去，请稍后再回复一次。" % card
    info = load_cards().get(card) or {"project": ctx.store.pid, "line": lid, "node": nid, "options": opts}
    try:
        save_card(card, dict(info, status="overruled", choice=k))
    except OSError:
        pass
    old = dec.get("choice")
    text = ("[用户推翻 %s] 原结论 选 %s，用户%s；请据此调整（需要时 git revert 相关提交），并 progress 记录" % (card, old, what))[:900]
    line = core.line_of(ctx.project, lid) or {}
    target = (((ctx.live or {}).get("routers") or {}).get(lid) or {}).get("handle") or line.get("router")
    sent = bool(target) and ctx.send_router(target, text)
    if sent:
        ctx.W({"kind": "nudge", "line": lid, "reason": "overrule", "summary": _short("用户推翻 %s，已通知 router" % card),
               "refs": {"target": target, "key": "overrule:%s:%s" % (card, entry_id(e))}})
    else:
        ctx.W({"kind": "observe", "line": lid, "summary": _short("用户推翻 %s，但没能通知 router" % card),
               "refs": {"key": "overrule_unsent:%s" % card}})
    return "applied", "%s 已记录推翻，已%s通知 router 调整。" % (card, "" if sent else "（没能）")


def run_round(store, actions: List[Dict[str, Any]], runner, write, read_spec, repo_path, send_router) -> List[Dict[str, Any]]:
    """Apply the inbox, then judge the planned decide nodes; at most PER_ROUND panels/explanations in total."""
    pending_actions = [a for a in actions if a.get("type") == "decide"]
    lock_path = core.home() / "decide.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = lock_path.open("a")
    except OSError:
        return [{"type": "decide", "error": "lock_unavailable"}]
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return [{"type": "decide", "skipped": "locked"}]
        ctx = Ctx(store, runner, write, read_spec, repo_path, send_router, [PER_ROUND])
        apply_inbox(ctx)
        for a in pending_actions:
            if ctx.budget[0] <= 0:
                break
            judge_one(ctx, a)
        return ctx.done
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            lock.close()
