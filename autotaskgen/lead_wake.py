#!/usr/bin/env python3
"""lead_wake: start one project-lead round when it is due (docs/LEAD.md).

Run by launchd every few minutes (template launchd/com.example.lead-wake.plist), and kicked at once by the Hermes
plugin lead-qq when the owner writes to the lead.
Due when: --force, or unread owner messages and >= inbox_min_gap_s since the last wake, or >= interval_s since the
last wake. One round at a time: skips while cycle.json says running (unless older than stale_running_s).
A round = a fresh agent process: `orca agent launch --prompt <wake prompt>` in config.worktree, after closing the
previous round's terminal. Nothing is typed into an existing terminal; the discussion terminal
(config discussion_terminal) is never written to, cleared or closed.
Watchdog (every run): a round that has not started its cycle WATCHDOG_S after launch is closed and relaunched once;
if the relaunch also does not start, one QQ alert with what its screen shows.
Off switch: touch <state dir>/WAKE_OFF (--force still wakes).
Log: <LEAD_ROOT>/log/wake.log. State: <state dir>/wake.json {last_wake, last_reason, round_terminal,
round_launched_at, round_reason, round_attempt, alerted_*}.
Project/state dir resolution and settings: the same as lead.py (--project, LEAD_PROJECT, LEAD_ROOT, LEAD_HOME).
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lead  # noqa: E402

DEFAULT_PROMPT = ("【项目负责人一轮｜唤醒原因：{reason}】读 {boot}，按其中步骤完成一轮"
                  "（第一步 `{lead} cycle start --reason {reason}`，最后一步 `{lead} cycle end ...`）。")
WATCHDOG_S = 900            # a launched round that has not started its cycle after this long is relaunched once
SUBVIEW_RE = re.compile(r"^\s*\u276f\s*Message @\S")          # "❯ Message @general-purpose…"
SUBVIEW_TITLE_RE = re.compile(r"\u2500\s+([^\u2500]+?)\s+\u2500+\s*$")   # "──── Daily literature scan ─"
RULE_RE = re.compile(r"^\s*\u2500{3,}")                         # the rules around the input box
PLACEHOLDER_RE = re.compile(r"^Try \"")                           # a fresh session's grey hint, not typed text
REALERT_S = 6 * 3600        # launch-failure / busy alerts repeat at most this often


def ts(s):
    t = lead.parse(s or "")
    return t.timestamp() if t else 0.0


def log(msg, pid=""):
    path = os.path.join(lead.lead_root(), "log", "wake.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s %s%s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "[%s] " % pid if pid else "", msg))
    except OSError:
        pass


def prompt_box(tail):
    """-> (subagent name or None, text sitting in the input box) from the last screen lines.
    On a subagent's transcript the rule above the box carries its name ("──── Daily literature scan 11-07 ─") and an
    empty box reads "Message @<agent>…"; once text is in the box only the titled rule is left (11-07: the lead sat
    there with "/clear /clear" in the box and four timer wakes went to the subagent)."""
    i = max([k for k, line in enumerate(tail) if line.lstrip().startswith("\u276f")] or [-1])
    if i < 0:
        return None, ""
    above = tail[i - 1] if i else ""
    m = SUBVIEW_TITLE_RE.search(above) if RULE_RE.search(above) else None
    sub = m.group(1).strip() if m else ("" if SUBVIEW_RE.search(tail[i]) else None)
    parts = [tail[i].lstrip()[1:].strip()]
    for line in tail[i + 1:]:
        if RULE_RE.search(line):
            break
        parts.append(line.strip())
    typed = " ".join(x for x in parts if x)
    if sub is not None or PLACEHOLDER_RE.search(typed):
        typed = ""
    return sub, typed


def due_reason(now, last_wake, n_unread, conf, force=False, wake_off=False):
    """-> 'manual' | 'inbox' | 'timer' | None. Pure; conf holds the interval keys (lead.CONFIG_DEFAULTS)."""
    if force:
        return "manual"
    if wake_off:
        return None
    gap = now - float(last_wake or 0)
    if n_unread and gap >= float(conf.get("inbox_min_gap_s") or 0):
        return "inbox"
    if gap >= float(conf.get("interval_s") or 0):
        return "timer"
    return None


def running_blocks(now, cycle, conf):
    """-> (blocks, age_s): a 'running' cycle blocks the wake until it is older than stale_running_s."""
    if (cycle or {}).get("state") != "running":
        return False, 0.0
    age = now - ts(cycle.get("started"))
    return age < float(conf.get("stale_running_s") or 0), age


def wake_prompt(ctx, reason):
    tpl = ctx.get("wake_prompt") or DEFAULT_PROMPT
    boot = ctx.boot
    home = os.path.expanduser("~")
    if boot.startswith(home + os.sep):
        boot = "~" + boot[len(home):]
    return tpl.format(reason=reason, project=ctx.pid, p=ctx.pid, boot=boot, lead=ctx.get("lead_cmd") or "lead")


class Waker:
    """One run of the waker. `ctx.runner` runs orca and the QQ sender; `sleep` is injectable for tests.

    v2 (11-07, docs/LEAD.md "怎么被唤醒"): every round is a fresh agent process started with
    `orca agent launch --prompt`; nothing is ever typed into an existing terminal. The discussion terminal
    (config discussion_terminal, formerly terminal) is never written to, cleared or closed."""

    def __init__(self, ctx, sleep=time.sleep, now=None):
        self.ctx, self.sleep = ctx, sleep
        self.now = time.time() if now is None else now
        self.prefix = ctx.get("alert_prefix") or ""

    def orca(self, *args, timeout=60):
        rc, out, err = self.ctx.runner([self.ctx.orca, *args, "--json"], timeout=timeout)
        try:
            return json.loads(out or "{}")
        except ValueError:
            return {"ok": False, "error": {"message": (err or out or "rc=%s" % rc)[:300]}}

    def qq(self, text):
        try:
            lead.qq_log(self.ctx, ("%s %s" % (self.prefix, text)).strip())
        except Exception:  # noqa: BLE001
            pass

    def protected(self):
        conf = self.ctx.conf
        return {h for h in (conf.get("discussion_terminal"), conf.get("terminal")) if h}

    def alive(self, handle):
        r = self.orca("terminal", "show", "--terminal", handle)
        t = (r.get("result") or {}).get("terminal") if r.get("ok") else None
        return bool(t and t.get("connected") and not t.get("orphaned"))

    def close(self, handle):
        """Close a round terminal (its whole tab). Never the discussion terminal. -> True when it was closed."""
        if not handle or handle in self.protected():
            return False
        r = self.orca("terminal", "close", "--terminal", handle, "--tab", timeout=60)
        return bool(r.get("ok"))

    def tail(self, handle, n=12):
        r = self.orca("terminal", "read", "--terminal", handle, "--limit", str(n), timeout=30)
        return ((r.get("result") or {}).get("terminal") or {}).get("tail") or [] if r.get("ok") else []

    def screen_cause(self, handle):
        """What the last 12 screen lines say about why no cycle started (for the QQ alert)."""
        tail = self.tail(handle) if handle else []
        if not tail:
            return "读不到屏幕（终端可能已经不在了）"
        sub, typed = prompt_box(tail)
        if sub is not None:
            return "停在子代理%s的对话框里" % ("「%s」" % sub if sub else "")
        if typed:
            return "输入框里有没发出去的文字「%s」" % typed[:40]
        body = [x.strip() for x in tail if x.strip() and not RULE_RE.search(x) and not x.lstrip().startswith("❯")]
        return "屏幕末尾：「%s」" % " / ".join(body[-3:])[:160] if body else "屏幕上没有可见原因"

    def launch(self, reason):
        """-> (handle or None, outcome kind, raw reply)."""
        conf = self.ctx.conf
        if not conf.get("worktree"):
            return None, "", {"ok": False, "error": {"message": "config.worktree is empty"}}
        r = self.orca("agent", "launch", "--agent", self.ctx.get("agent") or "claude", "--worktree",
                      "path:" + os.path.expanduser(conf["worktree"]), "--surface", "terminal",
                      "--prompt", wake_prompt(self.ctx, reason), timeout=180)
        res = r.get("result") or {}
        kind = str((res.get("outcome") or {}).get("kind") or "")
        handle = None
        for k in ("handle", "terminal"):
            v = res.get(k)
            if isinstance(v, str) and v.startswith("term_"):
                handle = v
            elif isinstance(v, dict) and str(v.get("handle", "")).startswith("term_"):
                handle = v["handle"]
        if not handle:
            m = re.search(r"term_[0-9A-Za-z-]+", json.dumps(r))
            handle = m.group(0) if m else None
        return (handle if r.get("ok", True) else None), kind, r

    def start_round(self, st, reason, attempt, why_closed=""):
        """Close the previous round terminal, launch a new one, record it. -> result dict."""
        ctx, now = self.ctx, self.now
        old = st.get("round_terminal")
        closed = self.close(old) if old else False
        new, kind, raw = self.launch(reason)
        log("launch %s attempt %d -> %s outcome=%s closed_prev=%s%s%s" % (
            reason, attempt, new or "-", kind or "?", (old or "-") if closed else ("no" if not old else "failed:%s" % old),
            " (%s)" % why_closed if why_closed else "", "" if new else " err=%s" % json.dumps(raw)[:300]), ctx.pid)
        if not new:
            if now - float(st.get("alerted_launch", 0)) > REALERT_S:
                self.qq("负责人一轮没开起来：新开会话失败（%s），需要你看一下 Orca" %
                        ((raw.get("error") or {}).get("message") or "无回执")[:120])
                st["alerted_launch"] = now
            lead.save_json(ctx.wake, st)
            return {"action": "failed", "reason": reason, "why": "launch_failed"}
        st.update({"round_terminal": new, "round_launched_at": now, "round_reason": reason, "round_attempt": attempt,
                   "last_wake": now, "last_reason": reason})
        lead.save_json(ctx.wake, st)
        return {"action": "launched", "reason": reason, "handle": new, "attempt": attempt, "closed_prev": closed}

    def run(self, force=False):
        """-> {"action": launched|skip|failed|alerted, ...}. Writes wake.json."""
        ctx, now = self.ctx, self.now
        conf = ctx.conf
        st = lead.load_json(ctx.wake, {}) or {}
        cy = lead.load_json(ctx.cycle, {}) or {}
        started = ts(cy.get("started"))
        rt, rl = st.get("round_terminal"), float(st.get("round_launched_at") or 0)
        wake_off = os.path.exists(os.path.join(ctx.home, "WAKE_OFF"))

        # watchdog, on every run: a launched round that has not started its cycle within WATCHDOG_S. WAKE_OFF pauses it
        # too, except for a round someone started by hand (--force)
        guard = not wake_off or st.get("round_reason") == "manual"
        if rt and rl and started < rl and now - rl > WATCHDOG_S and not force and guard:
            if int(st.get("round_attempt") or 1) < 2:
                cause = self.screen_cause(rt)
                log("watchdog: %s launched %.0f min ago, no cycle (%s); relaunching once" % (rt, (now - rl) / 60, cause),
                    ctx.pid)
                return self.start_round(st, st.get("round_reason") or "timer", 2, "watchdog: " + cause)
            if st.get("alerted_nostart") != rl:
                cause = self.screen_cause(rt)
                self.qq("负责人一轮连续两次没开起来（最后一次 %s 新开的 %s，过了 %d 分钟还没开始）：%s。请看一下这个终端" % (
                    ctx.show(lead.dt.datetime.fromtimestamp(rl, lead.dt.timezone.utc)), rt, (now - rl) // 60, cause))
                st["alerted_nostart"] = rl
                lead.save_json(ctx.wake, st)
                log("watchdog: %s still no cycle after the relaunch (%s); alerted" % (rt, cause), ctx.pid)
                return {"action": "alerted", "why": "no_cycle_twice"}

        last = float(st.get("last_wake", 0) or 0)
        n_unread = len(lead.unread(lead.read_jsonl(ctx.inbox)))
        reason = due_reason(now, last, n_unread, conf, force, wake_off)
        if not reason:
            would = wake_off and due_reason(now, last, n_unread, conf)
            if would and st.get("paused_logged") != last:              # one line per pause, not one every run
                log("skip %s: WAKE_OFF present, no round started" % would, ctx.pid)
                st["paused_logged"] = last
                lead.save_json(ctx.wake, st)
            return {"action": "skip", "why": "wake_off" if wake_off else "not_due"}
        blocks, age = running_blocks(now, cy, conf)
        if blocks:
            log("skip %s: cycle %s running %.0f min" % (reason, cy.get("n"), age / 60), ctx.pid)
            return {"action": "skip", "reason": reason, "why": "running"}
        if rt and rl and started < rl and now - rl <= WATCHDOG_S and not force:
            log("skip %s: round %s launched %.0f min ago, cycle not started yet" % (reason, rt, (now - rl) / 60), ctx.pid)
            return {"action": "skip", "reason": reason, "why": "starting"}
        why = ""
        if cy.get("state") == "running":
            why = "cycle %s running %.0f min, stale" % (cy.get("n"), age / 60)
            self.qq("负责人上一轮（第 %s 轮）开始 %.1f 小时还没结束，已关掉它的终端重新开一轮" % (cy.get("n"), age / 3600))
        return self.start_round(st, reason, 1, why)


def main(argv=None, runner=lead.run, sleep=time.sleep):
    p = argparse.ArgumentParser(prog="lead_wake", description="wake the project-lead session when due (docs/LEAD.md)")
    p.add_argument("--project", default=None)
    p.add_argument("--home", default=None)
    p.add_argument("--force", action="store_true", help="wake now (ignores intervals and WAKE_OFF, not a running cycle)")
    a = p.parse_args(argv)
    ctx = lead.Ctx.load(a.project, a.home, runner=runner)
    os.makedirs(ctx.home, exist_ok=True)
    import fcntl  # POSIX only; imported here so lead.py stays importable everywhere
    lockf = open(os.path.join(ctx.home, ".wake.lock"), "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return {"action": "skip", "why": "locked"}
    try:
        return Waker(ctx, sleep=sleep).run(force=a.force)
    finally:
        lockf.close()


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False))
