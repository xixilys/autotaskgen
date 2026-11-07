#!/usr/bin/env python3
"""lead_wake: start one project-lead cycle in the lead's Orca terminal when it is due (docs/LEAD.md).

Run by launchd every few minutes (template launchd/com.example.lead-wake.plist), and kicked at once by the Hermes
plugin lead-qq when the owner writes to the lead.
Due when: --force, or unread owner messages and >= inbox_min_gap_s since the last wake, or >= interval_s since the
last wake. Never interrupts: skips while cycle.json says running (unless older than stale_running_s) or the TUI is
not idle. Missing/closed terminal -> launches a fresh agent terminal in config.worktree, saves its handle, alerts by QQ.
A wake = "/clear", then one prompt telling the session to follow <state dir>/BOOT.md.
Off switch: touch <state dir>/WAKE_OFF (--force still wakes).
Log: <LEAD_ROOT>/log/wake.log. State: <state dir>/wake.json {last_wake, last_reason, busy_since?, alerted_*}.
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
NOSTART_GAP_S = 1800        # a wake that has not turned into a cycle after this long gets one alert
SUBVIEW_RE = re.compile(r"^\s*\u276f\s*Message @\S")          # "❯ Message @general-purpose…"
SUBVIEW_TITLE_RE = re.compile(r"\u2500\s+([^\u2500]+?)\s+\u2500+\s*$")   # "──── Daily literature scan ─"
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
    """One wake attempt. `ctx.runner` runs orca and the QQ sender; `sleep` is injectable for tests."""

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

    def terminal_ok(self, handle):
        r = self.orca("terminal", "show", "--terminal", handle)
        t = (r.get("result") or {}).get("terminal") if r.get("ok") else None
        if not t or not t.get("connected") or t.get("orphaned"):
            return None
        return t

    def subagent_view(self, handle):
        """Name of the subagent whose transcript the TUI shows ("" if unnamed), or None when on the main session.
        Claude Code's input box reads "Message @<agent>…" there; a wake typed into it never reaches the lead."""
        r = self.orca("terminal", "read", "--terminal", handle, "--limit", "8", timeout=30)
        tail = ((r.get("result") or {}).get("terminal") or {}).get("tail") or [] if r.get("ok") else []
        for i, line in enumerate(tail):
            if SUBVIEW_RE.search(line):
                prev = tail[i - 1] if i else ""
                m = SUBVIEW_TITLE_RE.search(prev)
                return m.group(1).strip() if m else ""
        return None

    def launch(self):
        conf = self.ctx.conf
        if not conf.get("worktree"):
            return None, {"ok": False, "error": {"message": "config.worktree is empty"}}
        r = self.orca("agent", "launch", "--agent", self.ctx.get("agent") or "claude", "--worktree",
                      "path:" + os.path.expanduser(conf["worktree"]), "--surface", "terminal", timeout=120)
        res = r.get("result") or {}
        handle = None
        for k in ("handle", "terminal"):
            v = res.get(k)
            if isinstance(v, str) and v.startswith("term_"):
                handle = v
            elif isinstance(v, dict) and str(v.get("handle", "")).startswith("term_"):
                handle = v["handle"]
        if not handle:
            blob = json.dumps(r)
            i = blob.find("term_")
            if i >= 0:
                j = i
                while j < len(blob) and (blob[j].isalnum() or blob[j] in "_-"):
                    j += 1
                handle = blob[i:j]
        return handle, r

    def run(self, force=False):
        """-> {"action": woke|skip|failed, "reason"?, "why"?, "handle"?}. Writes wake.json/config.json as needed."""
        ctx, now = self.ctx, self.now
        conf = ctx.conf
        st = lead.load_json(ctx.wake, {}) or {}
        last = float(st.get("last_wake", 0) or 0)
        n_unread = len(lead.unread(lead.read_jsonl(ctx.inbox)))
        wake_off = os.path.exists(os.path.join(ctx.home, "WAKE_OFF"))
        reason = due_reason(now, last, n_unread, conf, force, wake_off)
        if not reason:
            return {"action": "skip", "why": "wake_off" if wake_off else "not_due"}
        cy = lead.load_json(ctx.cycle, {}) or {}
        blocks, age = running_blocks(now, cy, conf)
        if blocks:
            log("skip %s: cycle %s running %.0f min" % (reason, cy.get("n"), age / 60), ctx.pid)
            return {"action": "skip", "reason": reason, "why": "running"}
        if cy.get("state") == "running":
            log("cycle %s stale (%.0f min), waking anyway" % (cy.get("n"), age / 60), ctx.pid)
        # an earlier wake that never turned into a cycle: say so once
        if last and ts(cy.get("started")) < last and now - last > NOSTART_GAP_S and st.get("alerted_nostart") != last:
            self.qq("上次唤醒（%s）后会话没有开始一轮，请看一下终端 %s" % (ctx.show(lead.dt.datetime.fromtimestamp(
                last, lead.dt.timezone.utc)), conf.get("terminal")))
            st["alerted_nostart"] = last
        handle = conf.get("terminal")
        t = self.terminal_ok(handle) if handle else None
        fresh = False
        if not t:
            new, raw = self.launch()
            if not new:
                log("launch failed: %s" % json.dumps(raw)[:300], ctx.pid)
                if now - float(st.get("alerted_launch", 0)) > REALERT_S:
                    self.qq("负责人终端 %s 不在了，自动新开也失败，需要你看一下 Orca" % (handle or "（未配置）"))
                    st["alerted_launch"] = now
                lead.save_json(ctx.wake, st)
                return {"action": "failed", "reason": reason, "why": "launch_failed"}
            log("terminal %s missing; launched %s" % (handle, new), ctx.pid)
            self.qq("原终端 %s 不在了，已在 %s 新开负责人会话 %s" % (
                handle or "（未配置）", os.path.basename(os.path.normpath(conf.get("worktree") or "")) or "?", new))
            conf["terminal"] = handle = new
            ctx.save_conf()
            fresh = True
            self.sleep(20)
        elif t.get("agentIdentity") not in (None, ctx.get("agent") or "claude"):
            log("terminal %s runs %s, not %s; refuse" % (handle, t.get("agentIdentity"), ctx.get("agent")), ctx.pid)
            return {"action": "skip", "reason": reason, "why": "wrong_agent"}
        w = self.orca("terminal", "wait", "--terminal", handle, "--for", "tui-idle", "--timeout-ms", "8000", timeout=30)
        if not (w.get("ok") and ((w.get("result") or {}).get("wait") or {}).get("satisfied")):
            busy_since = float(st.get("busy_since") or now)
            st["busy_since"] = busy_since
            if now - busy_since > float(conf.get("busy_alert_s") or 0) and now - float(st.get("alerted_busy", 0)) > REALERT_S:
                self.qq("负责人终端已经忙了 %.1f 小时，没法开始新一轮" % ((now - busy_since) / 3600))
                st["alerted_busy"] = now
            lead.save_json(ctx.wake, st)
            log("skip %s: terminal busy" % reason, ctx.pid)
            return {"action": "skip", "reason": reason, "why": "busy"}
        st.pop("busy_since", None)
        sub = self.subagent_view(handle)
        if sub is not None:
            # the TUI shows a subagent's transcript: anything typed now goes to that subagent, not the lead
            if now - float(st.get("alerted_subview", 0)) > REALERT_S:
                self.qq("负责人终端停在子代理%s的对话框里，唤醒会发给子代理而不是负责人；请在终端里切回主会话"
                        "（或停掉这个子代理）" % ("「%s」" % sub if sub else ""))
                st["alerted_subview"] = now
            lead.save_json(ctx.wake, st)
            log("skip %s: terminal shows subagent view %s" % (reason, sub or "?"), ctx.pid)
            return {"action": "skip", "reason": reason, "why": "subagent_view"}
        if not fresh:
            r = self.orca("terminal", "send", "--terminal", handle, "--text", "/clear", "--enter", timeout=30)
            if not r.get("ok"):
                log("clear failed: %s" % json.dumps(r)[:300], ctx.pid)
                lead.save_json(ctx.wake, st)
                return {"action": "failed", "reason": reason, "why": "clear_failed"}
            self.sleep(3)
            self.orca("terminal", "wait", "--terminal", handle, "--for", "tui-idle", "--timeout-ms", "30000", timeout=45)
        r = self.orca("terminal", "send", "--terminal", handle, "--text", wake_prompt(ctx, reason), "--enter",
                      "--wait-submit", "20", timeout=60)
        ok = bool(r.get("ok"))
        log("wake %s -> %s ok=%s %s" % (reason, handle, ok, json.dumps(r.get("result") or r.get("error"))[:200]), ctx.pid)
        if ok:
            st["last_wake"] = now
            st["last_reason"] = reason
        lead.save_json(ctx.wake, st)
        return {"action": "woke" if ok else "failed", "reason": reason, "handle": handle,
                **({} if ok else {"why": "send_failed"})}


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
    main()
