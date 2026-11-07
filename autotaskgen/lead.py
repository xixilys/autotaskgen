#!/usr/bin/env python3
"""lead: helper CLI for a project-lead session (docs/LEAD.md). Python stdlib only; nothing here calls a model.

The lead is one long-lived agent session (Claude Code in an Orca terminal) that is woken every few hours by
lead_wake.py, reads the framework's own numbers, acts first and reports after, and keeps a ledger of every
adjustment it makes, each with a checkable done_when and a recheck time.

State directory (default <LEAD_ROOT>/<project>, LEAD_ROOT default ~/.lead; ideally a local git repo):
  config.json   worktree, discussion terminal, wake intervals, optional paths (see CONFIG_DEFAULTS)
  BOOT.md       the per-cycle procedure the session follows (template: templates/lead/BOOT.md)
  HANDOFF.md    rolling handoff, rewritten by the session at the end of every cycle
  ledger.jsonl  adjustments/questions the lead issued, each with done_when + check_at
  inbox.jsonl   owner messages (Hermes plugin lead-qq) and the lead's replies
  cycle.json    {state: running|done, n, started, ended, reason, summary, verdict, report}
  status.json   lead-status/v1, written by `cycle end` and copied to <progress>/<pid>/lead/ for the board
  reports/      one markdown report per cycle (written by the session)

Subcommands (all print plain text):
  glance                      compact project numbers (project-review dossier + live.json + ledger + inbox)
  ledger add|check|user|list  manage the adjustment ledger
  inbox list|ack              owner messages
  qq TEXT|-                   send a QQ message to the owner through Hermes (logged in the inbox as dir=out)
  cycle start|end             mark a cycle; `end` writes status.json, copies it to the board, commits the state dir
  init                        create the state dir: config.json + BOOT.md rendered from templates/lead/BOOT.md

Project resolution: --project, else LEAD_PROJECT, else the only <LEAD_ROOT>/<pid>/config.json there is.
Settings: command line > environment > config.json > defaults (see docs/LEAD.md "Configuration").
Never reads any holdout directory; there is no option to point it at one.
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys

STATUS_SCHEMA = "lead-status/v1"
LEDGER_STATES = ("open", "done", "escalated", "obsolete")
VERDICTS = ("done", "not_done", "obsolete", "cannot_tell")

# config.json keys (all optional except worktree, which lead_wake needs) and their defaults.
CONFIG_DEFAULTS = {
    "discussion_terminal": "",    # Orca terminal the owner talks to the lead in; lead_wake never writes to it
    "worktree": "",               # worktree path a fresh lead session is launched in
    "interval_s": 7200,           # timer wake
    "inbox_min_gap_s": 600,       # earliest wake after the previous one when an owner message is waiting
    "stale_running_s": 10800,     # a 'running' cycle older than this no longer blocks a wake
    "progress_home": "",          # local progress store (dossiers); default $PROGRESS_HOME or ~/.progress
    "board_host": "",             # ssh host that serves the board; "" = the board reads the local progress_home
    "board_progress_home": ".progress",  # progress store on board_host (relative = under the remote home)
    "hermes_bin": "",             # default $HERMES_BIN, else `hermes` on PATH, else ~/.local/bin/hermes
    "qq_conf": "",                # default <progress_home>/qq.json {"target","owner"}
    "orca_bin": "",               # default $ORCA_BIN, else `orca` on PATH
    "agent": "claude",            # agent each lead round is launched with
    "lead_cmd": "lead",           # how the session calls this CLI (several projects: "lead --project <pid>")
    "wake_prompt": "",            # override the wake prompt; placeholders {reason} {project} {boot} {lead}
    "alert_prefix": "[负责人]",    # prefix of the QQ alerts lead_wake sends
    "utc_offset_h": None,         # display times in this UTC offset; default the machine's local time
    "git_name": "lead",           # identity for the state dir commit at cycle end
    "git_email": "lead@localhost",
}


# ------------------------------------------------------------------ time / io helpers

def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t=None):
    return (t or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s):
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if raw:
                    try:
                        out.append(json.loads(raw))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path, doc):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def run(argv, timeout=60, stdin=None):
    """Default runner: (rc, stdout, stderr); never raises. Tests pass their own."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=stdin)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as e:  # noqa: BLE001
        return 127, "", str(e)


# ------------------------------------------------------------------ context

def lead_root():
    return os.path.expanduser(os.environ.get("LEAD_ROOT") or "~/.lead")


def resolve_project(explicit=None):
    pid = explicit or os.environ.get("LEAD_PROJECT")
    if pid:
        return pid
    found = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(lead_root(), "*", "config.json")))
    if len(found) == 1:
        return found[0]
    sys.exit("lead: set LEAD_PROJECT or pass --project (projects under %s: %s)" % (lead_root(), ", ".join(found) or "none"))


class Ctx:
    """Resolved paths and settings for one project. Build with Ctx.load(); tests may pass runner/clock."""

    def __init__(self, pid, home, conf, runner=run, clock=now):
        self.pid, self.home, self.conf, self.runner, self.clock = pid, home, conf, runner, clock
        self.config_path = os.path.join(home, "config.json")
        self.ledger = os.path.join(home, "ledger.jsonl")
        self.inbox = os.path.join(home, "inbox.jsonl")
        self.cycle = os.path.join(home, "cycle.json")
        self.status = os.path.join(home, "status.json")
        self.wake = os.path.join(home, "wake.json")
        self.boot = os.path.join(home, "BOOT.md")

    @classmethod
    def load(cls, pid=None, home=None, runner=run, clock=now):
        pid = resolve_project(pid)
        if "/" in pid or pid.startswith("."):
            sys.exit("lead: bad project id %r" % pid)
        home = os.path.expanduser(home or os.environ.get("LEAD_HOME") or os.path.join(lead_root(), pid))
        conf = dict(CONFIG_DEFAULTS)
        conf.update(load_json(os.path.join(home, "config.json"), {}) or {})
        return cls(pid, home, conf, runner, clock)

    def get(self, key, env=None):
        if env and os.environ.get(env):
            return os.environ[env]
        v = self.conf.get(key)
        return CONFIG_DEFAULTS.get(key) if v is None else v

    @property
    def progress_home(self):
        return os.path.expanduser(self.get("progress_home", "LEAD_PROGRESS_HOME") or os.environ.get("PROGRESS_HOME")
                                  or "~/.progress")

    @property
    def board_host(self):
        return self.get("board_host", "LEAD_BOARD_HOST") or ""

    @property
    def board_progress_home(self):
        return self.get("board_progress_home", "LEAD_BOARD_PROGRESS_HOME") or ".progress"

    @property
    def dossier_glob(self):
        return os.path.join(self.progress_home, self.pid, "project_review", "dossier-*.json")

    @property
    def qq_conf(self):
        return os.path.expanduser(self.get("qq_conf", "LEAD_QQ_CONF") or os.path.join(self.progress_home, "qq.json"))

    @property
    def hermes(self):
        return os.path.expanduser(self.get("hermes_bin", "HERMES_BIN") or shutil.which("hermes")
                                  or "~/.local/bin/hermes")

    @property
    def orca(self):
        return os.path.expanduser(self.get("orca_bin", "ORCA_BIN") or shutil.which("orca") or "orca")

    def save_conf(self):
        on_disk = load_json(self.config_path, {}) or {}
        on_disk.update({k: v for k, v in self.conf.items() if k in on_disk or v != CONFIG_DEFAULTS.get(k)})
        save_json(self.config_path, on_disk)

    def show(self, s):
        """UTC ISO string or datetime -> 'MM-DD HH:MM' in utc_offset_h (or local time)."""
        t = parse(s) if isinstance(s, str) else s
        if not t:
            return "?"
        off = self.conf.get("utc_offset_h")
        if off is None and os.environ.get("LEAD_UTC_OFFSET_H"):
            off = os.environ["LEAD_UTC_OFFSET_H"]
        if off is not None:
            try:
                return (t + dt.timedelta(hours=float(off))).strftime("%m-%d %H:%M")
            except (TypeError, ValueError):
                pass
        return t.astimezone().strftime("%m-%d %H:%M")


# ------------------------------------------------------------------ glance

def latest_dossier(ctx):
    files = sorted(glob.glob(ctx.dossier_glob))
    return (files[-1], load_json(files[-1], {}) or {}) if files else (None, {})


def fetch_live(ctx):
    if not ctx.board_host:
        return load_json(os.path.join(ctx.progress_home, ctx.pid, "live.json"), {}) or {}
    rc, out, _ = ctx.runner(["ssh", "-o", "ConnectTimeout=8", ctx.board_host,
                             "cat %s/%s/live.json" % (ctx.board_progress_home, ctx.pid)], timeout=30)
    try:
        return json.loads(out) if rc == 0 else {}
    except ValueError:
        return {}


def pct(x):
    return "%d%%" % round(100 * x) if isinstance(x, (int, float)) else "?"


def _f1(x):
    return "%.1f" % x if isinstance(x, (int, float)) else "—"


def dossier_lines(ctx, path, d):
    lines = []
    g = parse(d.get("generated_at", ""))
    age = (ctx.clock() - g).total_seconds() / 3600 if g else None
    lines.append("== 项目档案 %s（%s 生成，%s 小时前）" % (os.path.basename(path), ctx.show(d.get("generated_at")), _f1(age)))
    for h in (d.get("headline") or [])[:8]:
        lines.append("- " + str(h.get("text", "") if isinstance(h, dict) else h))
    for s in d.get("stages") or []:
        lines.append("  %s 条目 %s/%s 满足，24h 关闭 %s，里程碑确认 %s/%s，距上次关闭 %s 小时" % (
            s.get("stage"), s.get("met"), s.get("total"), s.get("closed_24h"), s.get("milestones_confirmed"),
            s.get("milestones"), _f1(s.get("hours_since_last_closed"))))
    cp = d.get("critical_path") or {}
    if cp:
        lines.append("关键路径：%s；距上次里程碑确认 %s 小时" % (
            " → ".join("%s(缺%s/%s)" % (c.get("id"), c.get("gap"), c.get("total")) for c in cp.get("chain") or []),
            _f1(cp.get("hours_since_confirm"))))
    par = d.get("parallel") or {}
    if par:
        lines.append("并发（24h，平均/零并发占比）：" + "，".join(
            "%s %s/%s" % (k, _f1((v or {}).get("avg")), pct((v or {}).get("zero_share"))) for k, v in par.items()))
    idle = d.get("idle") or {}
    for kind, label in (("waits", "空等里程碑"), ("nowork", "没活")):
        for m, v in (idle.get(kind) or {}).items():
            lines.append("  %s %s：%s 线时（%s）" % (label, m, _f1(v.get("line_hours", 0)), ",".join(v.get("lines") or [])))
    ga = d.get("graph_audit") or {}
    if ga.get("blocked_near_done"):
        lines.append("被依赖图挡住的近完成项：" + "，".join(
            "%s(等 %s)" % (b.get("milestone"), ",".join(b.get("unmet_after") or [])) for b in ga["blocked_near_done"]))
    if ga.get("redundant"):
        lines.append("疑似多余依赖：" + json.dumps(ga["redundant"], ensure_ascii=False)[:300])
    ov = (d.get("overhead") or {}).get("by_line") or {}
    if ov:
        lines.append("开销（审查+返修）/总耗时：" + "，".join("%s %s" % (k, pct(v)) for k, v in ov.items()))
    pa = d.get("partial") or {}
    if pa:
        lines.append("PARTIAL/交付：" + "，".join("%s %s/%s" % (k, v.get("partial"), v.get("total")) for k, v in pa.items()))
    opn = [x for x in d.get("decision_delay") or [] if x.get("open")]
    if opn:
        lines.append("未决的决策：" + "；".join("%s（%sh）" % (str(x.get("what", ""))[:60], _f1(x.get("hours"))) for x in opn[:5]))
    flags = d.get("flags") or []
    if flags:
        lines.append("档案旗标 %d 条（前 6）：" % len(flags))
        for f in flags[:6]:
            lines.append("  [%s→%s] %s" % (f.get("level"), f.get("to"), f.get("text")))
    return lines


def worker_phase(w):
    """Same rule as progress_core.worker_phase (lead.py stays standalone): running = busy and task not settled."""
    if w.get("lingering") is True:
        return "lingering"
    return "running" if w.get("busy") is True else "idle"


def live_lines(ctx, live):
    ws = live.get("workers") or []
    ws = [w for w in ws if isinstance(w, dict)] if isinstance(ws, list) else []
    by = {"running": {}, "idle": {}, "lingering": {}}
    for w in ws:
        k = w.get("line") or w.get("router") or "?"
        b = by[worker_phase(w)]
        b[k] = b.get(k, 0) + 1
    n = {p: sum(v.values()) for p, v in by.items()}
    head = "== 现在（live %s）：在跑 worker %d 个 %s（只数屏幕在忙且任务没结束的）" % (
        ctx.show(live.get("generated_at")), n["running"], json.dumps(by["running"], ensure_ascii=False))
    if n["idle"]:
        head += "；停着 %d 个 %s（任务没结束、屏幕空闲或不明）" % (n["idle"], json.dumps(by["idle"], ensure_ascii=False))
    if n["lingering"]:
        head += "；已结束还开着 %d 个 %s（不算在跑）" % (n["lingering"], json.dumps(by["lingering"], ensure_ascii=False))
    lines = [head]
    for host, r in sorted((live.get("resources") or {}).items()):
        if not isinstance(r, dict):
            continue
        disks = ",".join("%s %sG" % (x.get("mount"), x.get("avail_gb")) for x in r.get("disks") or [])
        mem = r.get("mem_avail_gb")
        lines.append("  %s：可用内存 %s，盘 %s" % (host, "%sG" % mem if mem is not None else "—", disks or "—"))
    for ln, m in (live.get("metrics") or {}).items():
        cp = (m or {}).get("critical_path") or {}
        ru = (m or {}).get("ready_undispatched") or {}
        if cp.get("stall_min") or ru.get("nodes"):
            lines.append("  %s 关键路径停 %s 分钟，就绪未派 %s 个" % (ln, cp.get("stall_min"), ru.get("nodes")))
    if live.get("errors"):
        lines.append("  live 错误：" + json.dumps(live["errors"], ensure_ascii=False)[:300])
    return lines


def due_rows(ctx, rows):
    t = ctx.clock()
    return [r for r in rows if r.get("status") == "open" and parse(r.get("check_at", "")) and parse(r["check_at"]) <= t]


def unread(rows):
    return [m for m in rows if m.get("dir") == "in" and not m.get("acked")]


def live_missing_hint(ctx):
    """One line saying where live.json was looked for and what to set (11-07: on the Mac with board_host empty the
    「现在」section was easy to miss as gone)."""
    if ctx.board_host:
        return "== 现在：读不到在跑快照（ssh %s cat %s/%s/live.json 失败或文件不存在），在跑 worker 数未知" % (
            ctx.board_host, ctx.board_progress_home, ctx.pid)
    return ("== 现在：读不到在跑快照 %s，在跑 worker 数未知。看板在另一台机器上就在 config.json 设 board_host"
            "（例如 \"wsl\"）或设环境变量 LEAD_BOARD_HOST" % os.path.join(ctx.progress_home, ctx.pid, "live.json"))


def glance_text(ctx):
    path, d = latest_dossier(ctx)
    lines = dossier_lines(ctx, path, d) if d else ["== 没有项目档案（%s）" % ctx.dossier_glob]
    live = fetch_live(ctx)
    if live:
        lines += live_lines(ctx, live)
    else:
        lines.append(live_missing_hint(ctx))
    rows = read_jsonl(ctx.ledger)
    op = [r for r in rows if r.get("status") == "open"]
    due = due_rows(ctx, rows)
    lines.append("== 台账：未结 %d 条，到期待查 %d 条%s" % (len(op), len(due), ("：" + "，".join(r["id"] for r in due)) if due else ""))
    ub = unread(read_jsonl(ctx.inbox))
    lines.append("== 你的 QQ 消息未处理 %d 条" % len(ub))
    for m in ub[:10]:
        lines.append("  [%s %s] %s" % (m.get("id"), ctx.show(m.get("at")), str(m.get("text", ""))[:200]))
    cy = load_json(ctx.cycle, {}) or {}
    lines.append("== 上一轮：%s，开始 %s，结束 %s" % (cy.get("state"), ctx.show(cy.get("started")), ctx.show(cy.get("ended"))))
    return "\n".join(lines)


def cmd_glance(ctx, _a):
    print(glance_text(ctx))


# ------------------------------------------------------------------ ledger

def next_id(rows, prefix):
    n = 0
    for r in rows:
        rid = str(r.get("id", ""))
        if rid.startswith(prefix) and rid[len(prefix):].isdigit():
            n = max(n, int(rid[len(prefix):]))
    return "%s%d" % (prefix, n + 1)


def ledger_add(ctx, to, what, done_when, kind="adjust", why="", check_h=6.0, baseline=None, sent=""):
    if not to or not what or not done_when:
        raise ValueError("ledger add needs --to, --what and --done-when")
    rows = read_jsonl(ctx.ledger)
    base = {}
    for kv in baseline or []:
        k, _, v = kv.partition("=")
        base[k] = v
    t = ctx.clock()
    r = {"id": next_id(rows, "L"), "at": iso(t), "to": to, "kind": kind, "what": what, "why": why,
         "done_when": done_when, "baseline": base, "check_at": iso(t + dt.timedelta(hours=check_h)),
         "status": "open", "checks": [], "user": [], "sent": sent}
    rows.append(r)
    write_jsonl(ctx.ledger, rows)
    return r


def ledger_check(ctx, rid, verdict, evidence="", recheck_h=6.0):
    """done/obsolete close the row; not_done twice -> escalated; cannot_tell (or a first not_done) reschedules."""
    if verdict not in VERDICTS:
        raise ValueError("verdict must be one of %s" % ", ".join(VERDICTS))
    rows = read_jsonl(ctx.ledger)
    t = ctx.clock()
    for r in rows:
        if r.get("id") == rid:
            r.setdefault("checks", []).append({"at": iso(t), "verdict": verdict, "evidence": evidence})
            if verdict in ("done", "obsolete"):
                r["status"] = verdict
            else:
                if verdict == "not_done":
                    misses = sum(1 for c in r["checks"] if c.get("verdict") == "not_done")
                    r["status"] = "escalated" if misses >= 2 else "open"
                r["check_at"] = iso(t + dt.timedelta(hours=recheck_h))
            write_jsonl(ctx.ledger, rows)
            return r
    raise KeyError(rid)


def ledger_user(ctx, rid, text):
    rows = read_jsonl(ctx.ledger)
    for r in rows:
        if r.get("id") == rid:
            r.setdefault("user", []).append({"at": iso(ctx.clock()), "text": text})
            write_jsonl(ctx.ledger, rows)
            return r
    raise KeyError(rid)


def ledger_list(ctx, want="open"):
    rows = read_jsonl(ctx.ledger)
    if want == "open":
        return [r for r in rows if r.get("status") in ("open", "escalated")]
    if want == "due":
        return due_rows(ctx, rows)
    return rows


def cmd_ledger(ctx, a):
    try:
        if a.op == "add":
            r = ledger_add(ctx, a.to, a.what, a.done_when, a.kind, a.why, a.check_h, a.baseline, a.sent)
            print(r["id"])
        elif a.op == "check":
            if not a.verdict:
                sys.exit("ledger check needs --verdict")
            r = ledger_check(ctx, a.id, a.verdict, a.evidence, a.recheck_h)
            print("%s -> %s" % (r["id"], r["status"]))
        elif a.op == "user":
            ledger_user(ctx, a.id, a.text)
            print("ok")
        else:
            for r in ledger_list(ctx, a.filter):
                last = r["checks"][-1]["verdict"] if r.get("checks") else "-"
                print("%s [%s|%s] →%s 查于%s 上次:%s\n   %s\n   落实标准：%s" % (
                    r.get("id"), r.get("status"), r.get("kind"), r.get("to"), ctx.show(r.get("check_at")), last,
                    str(r.get("what", ""))[:200], str(r.get("done_when", ""))[:200]))
    except KeyError:
        sys.exit("no such id %s" % a.id)
    except ValueError as e:
        sys.exit(str(e))


# ------------------------------------------------------------------ inbox / qq

def qq_send(ctx, text):
    """Send through `hermes send -t <target> -q <text>`; qq.json supplies the target (its values are never logged)."""
    qq = load_json(ctx.qq_conf, {}) or {}
    if not qq.get("target"):
        return False, "qq_unconfigured"
    text = text if len(text) <= 1800 else text[:1790] + "…"
    rc, out, err = ctx.runner([ctx.hermes, "send", "-t", qq["target"], "-q", text], timeout=90)
    return rc == 0, (err or out or "")[:200]


def qq_log(ctx, text):
    text = text.strip()
    ok, why = qq_send(ctx, text)
    rows = read_jsonl(ctx.inbox)
    rows.append({"id": next_id(rows, "O"), "dir": "out", "at": iso(ctx.clock()), "text": text[:1800], "ok": ok, "err": why})
    write_jsonl(ctx.inbox, rows)
    return ok, why


def cmd_qq(ctx, a):
    ok, why = qq_log(ctx, sys.stdin.read() if a.text == "-" else a.text)
    print("sent" if ok else "FAILED " + why)
    if not ok:
        sys.exit(1)


def inbox_ack(ctx, mid, note=""):
    rows = read_jsonl(ctx.inbox)
    hit = False
    for r in rows:
        if r.get("id") == mid:
            r["acked"] = iso(ctx.clock())
            r["handled"] = note
            hit = True
    if hit:
        write_jsonl(ctx.inbox, rows)
    return hit


def inbox_add(ctx, text, sender, key=""):
    """An inbound note from a framework script (not the human), e.g. the supervisor's split-router advice.
    Same row shape as lead-qq's; `key` dedupes (stored as message_id). -> id, or None when the key is already in."""
    rows = read_jsonl(ctx.inbox)
    if key and any(r.get("message_id") == key for r in rows):
        return None
    row = {"id": next_id(rows, "I"), "dir": "in", "at": iso(ctx.clock()), "text": text, "from": sender}
    if key:
        row["message_id"] = key
    os.makedirs(os.path.dirname(ctx.inbox), exist_ok=True)
    with open(ctx.inbox, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row["id"]


def cmd_inbox(ctx, a):
    if a.op == "add":
        if not a.text:
            sys.exit("inbox add needs --text")
        rid = inbox_add(ctx, a.text, a.sender, a.key)
        print(rid or "duplicate")
        return
    if a.op == "ack":
        if not inbox_ack(ctx, a.id, a.note):
            sys.exit("no such id %s" % a.id)
        print("ok")
        return
    for r in read_jsonl(ctx.inbox)[-a.n:]:
        if a.unread and (r.get("dir") != "in" or r.get("acked")):
            continue
        print("%s %s %s %s%s" % (r.get("id"), r.get("dir"), ctx.show(r.get("at")), str(r.get("text", ""))[:300].replace("\n", " "),
                                 "" if r.get("dir") == "out" or not r.get("acked") else "  [已处理：%s]" % (r.get("handled") or "")))


# ------------------------------------------------------------------ cycle / status

def cycle_start(ctx, reason="timer"):
    cy = load_json(ctx.cycle, {}) or {}
    cy = {"state": "running", "started": iso(ctx.clock()), "reason": reason, "n": int(cy.get("n", 0)) + 1}
    save_json(ctx.cycle, cy)
    return cy


def build_status(ctx, cy, rows):
    return {
        "schema": STATUS_SCHEMA, "updated": iso(ctx.clock()), "cycle": cy.get("n"), "verdict": cy.get("verdict"),
        "summary": cy.get("summary"), "report": cy.get("report"),
        "ledger": [{k: r.get(k) for k in ("id", "to", "kind", "what", "done_when", "check_at", "status")}
                   for r in rows if r.get("status") in ("open", "escalated")][-30:],
        "ledger_counts": {s: sum(1 for r in rows if r.get("status") == s) for s in LEDGER_STATES},
    }


def publish_status(ctx):
    """Copy status.json where the board reads it: <progress>/<pid>/lead/status.json, locally or on board_host."""
    if not ctx.board_host:
        dst = os.path.join(ctx.progress_home, ctx.pid, "lead", "status.json")
        try:
            if os.path.abspath(dst) != os.path.abspath(ctx.status):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(ctx.status, dst + ".tmp")
                os.replace(dst + ".tmp", dst)
            return True
        except OSError:
            return False
    rdir = "%s/%s/lead" % (ctx.board_progress_home, ctx.pid)
    rc, _, _ = ctx.runner(["ssh", "-o", "ConnectTimeout=8", ctx.board_host, "mkdir -p %s" % rdir], timeout=30)
    if rc == 0:
        rc, _, _ = ctx.runner(["scp", "-q", ctx.status, "%s:%s/status.json" % (ctx.board_host, rdir)], timeout=60)
    return rc == 0


def commit_state(ctx, msg):
    if not os.path.isdir(os.path.join(ctx.home, ".git")):
        return False
    ctx.runner(["git", "-C", ctx.home, "add", "-A"], timeout=60)
    rc, _, _ = ctx.runner(["git", "-C", ctx.home, "-c", "user.name=%s" % ctx.get("git_name"),
                           "-c", "user.email=%s" % ctx.get("git_email"), "commit", "-q", "-m", msg], timeout=60)
    return rc == 0


def cycle_end(ctx, verdict="", summary="", report=""):
    cy = load_json(ctx.cycle, {}) or {}
    cy.update({"state": "done", "ended": iso(ctx.clock()), "summary": summary, "verdict": verdict, "report": report})
    save_json(ctx.cycle, cy)
    status = build_status(ctx, cy, read_jsonl(ctx.ledger))
    save_json(ctx.status, status)
    ok = publish_status(ctx)
    commit_state(ctx, "cycle %s: %s" % (cy.get("n"), (summary or "")[:60]))
    return cy, status, ok


def cmd_cycle(ctx, a):
    if a.op == "start":
        print("cycle %d started" % cycle_start(ctx, a.reason)["n"])
        return
    cy, _, ok = cycle_end(ctx, a.verdict, a.summary, a.report)
    print("cycle %s done; board copy %s" % (cy.get("n"), "ok" if ok else "FAILED"))


# ------------------------------------------------------------------ init

PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def default_template():
    return os.environ.get("LEAD_TEMPLATE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), os.pardir, "templates", "lead", "BOOT.md")


def render(text, values):
    """Replace {name} for names in values; -> (text, sorted names left unresolved)."""
    left = set()

    def sub(m):
        if m.group(1) in values:
            return str(values[m.group(1)])
        left.add(m.group(1))
        return m.group(0)
    return PLACEHOLDER.sub(sub, text), sorted(left)


def init(ctx, conf_updates=None, values=None, template=None, force=False, git=True):
    """Create/extend the state dir. Existing config keys are kept unless given; BOOT.md is kept unless force."""
    os.makedirs(os.path.join(ctx.home, "reports"), exist_ok=True)
    on_disk = load_json(ctx.config_path, {}) or {}
    for k in ("discussion_terminal", "worktree", "interval_s", "inbox_min_gap_s", "stale_running_s"):
        on_disk.setdefault(k, CONFIG_DEFAULTS[k])
    on_disk.update({k: v for k, v in (conf_updates or {}).items() if v not in (None, "")})
    save_json(ctx.config_path, on_disk)
    ctx.conf.update(on_disk)
    left = []
    if force or not os.path.exists(ctx.boot):
        with open(template or default_template(), encoding="utf-8") as fh:
            text = fh.read()
        vals = {"project": ctx.pid, "state_dir": ctx.home, "lead": ctx.get("lead_cmd") or "lead"}
        vals.update(values or {})
        text, left = render(text, vals)
        with open(ctx.boot, "w", encoding="utf-8") as fh:
            fh.write(text)
    if git and not os.path.isdir(os.path.join(ctx.home, ".git")):
        ctx.runner(["git", "-C", ctx.home, "init", "-q"], timeout=30)
    return left


def cmd_init(ctx, a):
    upd = {"discussion_terminal": a.terminal, "worktree": a.worktree, "board_host": a.board_host}
    vals = {}
    for kv in a.set or []:
        k, _, v = kv.partition("=")
        vals[k.strip()] = v
    try:
        left = init(ctx, upd, vals, a.template, a.force, not a.no_git)
    except OSError as e:
        sys.exit("lead init: %s (pass --template or set LEAD_TEMPLATE)" % e)
    print("initialised %s" % ctx.home)
    if not ctx.board_host and not os.path.exists(os.path.join(ctx.progress_home, ctx.pid, "live.json")):
        print("WARNING: board_host is empty and %s does not exist: glance will show no running workers. If the board"
              " runs on another machine, re-run with --board-host <ssh host> or set board_host in %s"
              % (os.path.join(ctx.progress_home, ctx.pid, "live.json"), ctx.config_path))
    if left:
        print("BOOT.md placeholders still to fill: " + ", ".join("{%s}" % x for x in left))


# ------------------------------------------------------------------ cli

def build_parser():
    p = argparse.ArgumentParser(prog="lead", description="project-lead helper (docs/LEAD.md)")
    p.add_argument("--project", default=None, help="project id (default $LEAD_PROJECT or the only one under $LEAD_ROOT)")
    p.add_argument("--home", default=None, help="state dir (default $LEAD_HOME or $LEAD_ROOT/<project>)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("glance").set_defaults(fn=cmd_glance)
    lg = sub.add_parser("ledger")
    lg.add_argument("op", choices=["add", "check", "user", "list"])
    lg.add_argument("id", nargs="?")
    lg.add_argument("--to", default="", help="recipient, e.g. coordinator:<core id>, router:<line>, human, tooling")
    lg.add_argument("--kind", default="adjust", help="adjust|question|pro|human|tooling")
    lg.add_argument("--what", default="")
    lg.add_argument("--why", default="")
    lg.add_argument("--done-when", dest="done_when", default="")
    lg.add_argument("--check-h", dest="check_h", type=float, default=6)
    lg.add_argument("--baseline", nargs="*")
    lg.add_argument("--sent", default="", help="message request id / where it was sent")
    lg.add_argument("--verdict", choices=list(VERDICTS))
    lg.add_argument("--evidence", default="")
    lg.add_argument("--recheck-h", dest="recheck_h", type=float, default=6)
    lg.add_argument("--text", default="")
    lg.add_argument("--filter", choices=["all", "open", "due"], default="open")
    lg.set_defaults(fn=cmd_ledger)
    ib = sub.add_parser("inbox")
    ib.add_argument("op", choices=["list", "ack", "add"])
    ib.add_argument("id", nargs="?")
    ib.add_argument("--note", default="")
    ib.add_argument("--text", default="", help="add: the note")
    ib.add_argument("--from", dest="sender", default="supervisor", help="add: who sends it (a script, not the human)")
    ib.add_argument("--key", default="", help="add: dedupe key; a second add with the same key is dropped")
    ib.add_argument("--unread", action="store_true")
    ib.add_argument("-n", type=int, default=30)
    ib.set_defaults(fn=cmd_inbox)
    q = sub.add_parser("qq")
    q.add_argument("text")
    q.set_defaults(fn=cmd_qq)
    c = sub.add_parser("cycle")
    c.add_argument("op", choices=["start", "end"])
    c.add_argument("--reason", default="timer")
    c.add_argument("--summary", default="")
    c.add_argument("--verdict", default="", help="on_track|at_risk|off_track")
    c.add_argument("--report", default="")
    c.set_defaults(fn=cmd_cycle)
    it = sub.add_parser("init")
    it.add_argument("--terminal", default="", help="Orca handle of the terminal the owner talks to the lead in (lead_wake never writes to it)")
    it.add_argument("--worktree", default="", help="worktree a fresh lead session is launched in")
    it.add_argument("--board-host", dest="board_host", default="", help="ssh host of the board (empty = local)")
    it.add_argument("--set", nargs="*", help="BOOT.md placeholder values, name=value")
    it.add_argument("--template", default=None, help="BOOT template (default templates/lead/BOOT.md or $LEAD_TEMPLATE)")
    it.add_argument("--force", action="store_true", help="re-render BOOT.md even if it exists")
    it.add_argument("--no-git", dest="no_git", action="store_true")
    it.set_defaults(fn=cmd_init)
    return p


def main(argv=None, runner=run, clock=now):
    a = build_parser().parse_args(argv)
    ctx = Ctx.load(a.project, a.home, runner=runner, clock=clock)
    a.fn(ctx, a)


if __name__ == "__main__":
    main()
