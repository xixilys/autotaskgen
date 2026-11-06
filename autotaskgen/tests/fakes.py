"""Fake orca / ssh / git runner for patrol and snapshot tests."""
import json
import os
import re

# patrol tests run with fake runners: the real project review (reads/writes the task cache, scp) stays off unless a
# test passes review_fn explicitly
os.environ["PROGRESS_PROJECT_REVIEW"] = "off"

SPEC_HEADER = {"route": {"agent": "codex", "model": "codex/gpt-6.1-sol", "effort": "high", "on": "wsl"},
               "expected_duration_minutes": 60, "write_scope": ["x"]}
SPEC_TEXT = json.dumps(SPEC_HEADER) + "\n\n# spec body\nline two\n"

START_OK = {"ok": True, "result": {
    "runId": "run_x", "taskId": "task_x1", "dispatchId": "ctx_x1", "state": "ready",
    "effects": [{"kind": "worktree", "action": "created", "id": "repo::/home/u/ws/l-a-p1"},
                {"kind": "terminal", "role": "agent", "action": "created", "id": "term_new-worker"}]}}


def df_text(*rows):
    """rows: (mount, avail_gb, used_pct) -> `df -Pk` output."""
    out = "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
    for m, gb, pct in rows:
        out += "/dev/sdx %d %d %d %d%% %s\n" % (int(gb * 1048576 * 100 / (100 - pct)), 1, int(gb * 1048576), pct, m)
    return out


DF_OK = df_text(("/", 400, 20))
MEM_OK = "MemAvailable:   16777216 kB\n"
# a scan block for a worktree that is safe to remove (clean, branch kept, nothing recent); override fields per path
WT_CLEAN = {"NEWER": "", "GITRC": "0", "DIRTY": "", "IGNORED": "", "COMMON": "/home/u/repo/.git",
            "GITDIR": "/home/u/repo/.git/worktrees/x", "BRANCH": "feat-x", "HEAD": "abc1234",
            "REFS": "refs/heads/feat-x", "BRANCH_OK": "1", "DU": "2516582"}   # DU in KiB: ~2.4 GiB
NO_RUN = "run_none"  # run id the fake lists for rows without a runId


class FakeRunner:
    def __init__(self, terminals=None, ps="", ps_rc=0, orca_rc=0, start=None, start_rc=0,
                 send_rc=0, spec=SPEC_TEXT, screen=None, wl_rows=None, wl_rc=0, wl_page=100,
                 shows=None, show_rc=0, runs=None, runs_rc=0, wl_bound=False, worktrees=None, wt_rc=0, reads=None, read_rc=0, df=None, df_rc=0,
                 release=None, release_rc=0, wsl_terminals=None, wsl_rc=0, close_rc=0,
                 wt_scan=None, scan_rc=0, wt_rm_rc=0, branch_after=True):
        # df: {"wsl"|"mac": df/meminfo text}; default healthy
        self.df, self.df_rc = df or {}, df_rc
        # worker-release reply (result object) and exit code
        self.release = {"ok": True, "result": {"status": "released"}} if release is None else release
        self.release_rc = release_rc
        # `terminal list --environment wsl` rows / exit code; `terminal close` exit code
        self.wsl_terminals, self.wsl_rc, self.close_rc = wsl_terminals or [], wsl_rc, close_rc
        # worktree cleanup: wt_scan = {path: {field: value}} overriding WT_CLEAN for that path (None path = absent);
        # `orca worktree rm` exit code; whether the branch still exists afterwards
        self.wt_scan, self.scan_rc, self.wt_rm_rc, self.branch_after = wt_scan or {}, scan_rc, wt_rm_rc, branch_after
        self.calls = []
        self.terminals = terminals or []
        self.ps, self.ps_rc, self.orca_rc = ps, ps_rc, orca_rc
        self.start = START_OK if start is None else start
        self.start_rc, self.send_rc, self.spec = start_rc, send_rc, spec
        self.screen = screen
        # orca dispatches: wl_rows = {terminalState: [full worker-list rows]}; shows = {dispatchId: result}
        self.wl_rows, self.wl_rc, self.wl_page = wl_rows or {}, wl_rc, wl_page
        self.shows, self.show_rc = shows or {}, show_rc
        self.runs, self.runs_rc, self.wl_bound = runs or [], runs_rc, wl_bound
        self.worktrees, self.wt_rc = worktrees or [], wt_rc
        # worker-read: {dispatchId: {tail: [...], liveness: 'live', terminal: 'running'}}
        self.reads, self.read_rc = reads or {}, read_rc

    def kinds(self, word):
        return [c for c in self.calls if word in c]

    def __call__(self, argv, input_text=None, timeout=60):
        self.calls.append(argv)
        if argv[0] == "git":
            return (0, self.spec, "") if self.spec is not None else (128, "", "fatal: bad ref")
        if argv[0] == "df":
            return self.df_rc, self.df.get("mac", DF_OK), "" if self.df_rc == 0 else "df down"
        if argv[0] == "ssh":
            if "df -Pk" in argv[-1]:
                return self.df_rc, self.df.get("wsl", DF_OK + MEM_OK), "" if self.df_rc == 0 else "ssh: unreachable"
            if "@@WT" in argv[-1]:
                if self.scan_rc:
                    return self.scan_rc, "", "ssh: scan failed"
                out = ""
                for path in re.findall(r"p=(\S+); echo", argv[-1]):
                    path = path.strip("'")
                    out += "@@WT %s\n" % path
                    info = dict(WT_CLEAN, **(self.wt_scan.get(path) or {}))
                    if info.get("LINK"):
                        out += "LINK %s\n" % info["LINK"]
                        continue
                    if info.get("MISSING"):
                        out += "MISSING\n"
                        continue
                    out += "".join("%s %s\n" % (k, v) for k, v in info.items() if k != "MISSING" and v is not None)
                return 0, out, ""
            if "@@BR" in argv[-1]:
                ok = {True: "0", False: "1", "restored": "R"}[self.branch_after]
                return 0, "".join("@@BR %s %s\n" % (p, ok)
                                  for p in re.findall(r'echo "@@BR (\S+) 0"', argv[-1])), ""
            if "ps -ww" in argv[-1]:
                return self.ps_rc, self.ps if self.ps_rc == 0 else "", "" if self.ps_rc == 0 else "ssh: unreachable"
            if "project_review" in argv[-1]:
                return 0, "", ""
            return 255, "", "ssh: unreachable"
        if argv[0] == "scp":
            return 0, "", ""
        if "worker-list" in argv:
            if self.wl_rc:
                return self.wl_rc, "", "worker-list down"
            if "--run" in argv:
                rid = argv[argv.index("--run") + 1]
                allrows = [r for v in self.wl_rows.values() if v != "garbage" for r in v]
                rows = [r for r in allrows if (r.get("runId") or NO_RUN) == rid]
                if any(v == "garbage" for v in self.wl_rows.values()):
                    rows = "garbage"
                scope = {"run": rid, "source": "flag"}
            else:
                rows = self.wl_rows.get(argv[argv.index("--terminal-state") + 1], [])
                scope = {"run": "run_bound", "source": "bound"} if self.wl_bound else {"run": None, "source": "all"}
            if rows == "garbage":
                return 0, json.dumps({"result": {"workers": "nope"}}), ""
            off = int(argv[argv.index("--cursor") + 1]) if "--cursor" in argv else 0
            chunk = rows[off:off + self.wl_page]
            more = off + self.wl_page < len(rows)
            page = {"hasMore": more, "nextCursor": str(off + self.wl_page) if more else None, "total": len(rows)}
            return 0, json.dumps({"result": {"workers": chunk, "page": page, "scope": scope}}), ""
        if "worker-read" in argv:
            if self.read_rc:
                return self.read_rc, "", "read down"
            r = self.reads.get(argv[argv.index("--dispatch") + 1])
            if r is None:
                return 1, "", "no such dispatch"
            return 0, json.dumps({"result": {"status": {"worker": "ready", "terminal": r.get("terminal", "running"),
                                                         "liveness": r.get("liveness", "live")},
                                             "terminal": {"status": r.get("terminal", "running"),
                                                          "tail": r["tail"]}}}), ""
        if "worker-show" in argv:
            if self.show_rc:
                return self.show_rc, "", "show down"
            r = self.shows.get(argv[argv.index("--dispatch") + 1])
            return (0, json.dumps({"result": r}), "") if r else (1, "", "no such dispatch")
        if "run-list" in argv:
            if self.runs_rc:
                return self.runs_rc, "", "run-list down"
            runs = list(self.runs)
            have = {r.get("id") for r in runs}
            for v in self.wl_rows.values():  # every run that owns a row is listed
                for r in (v if v != "garbage" else [{}]):
                    rid = r.get("runId") or NO_RUN
                    if rid not in have:
                        have.add(rid)
                        runs.append({"id": rid})
            return 0, json.dumps({"result": {"runs": runs}}), ""
        if "worker-release" in argv:
            out = self.release if isinstance(self.release, str) else json.dumps(self.release)
            return self.release_rc, out, "" if self.release_rc == 0 else "release failed"
        if "worker-start" in argv:
            out = self.start if isinstance(self.start, str) else json.dumps(self.start)
            return self.start_rc, out, ""
        if "close" in argv and "terminal" in argv:
            return self.close_rc, json.dumps({"ok": self.close_rc == 0}), "" if self.close_rc == 0 else "close failed"
        if "send" in argv:
            return self.send_rc, json.dumps({"ok": self.send_rc == 0}), ""
        if "worktree" in argv and "rm" in argv:
            return self.wt_rm_rc, json.dumps({"ok": self.wt_rm_rc == 0}), "" if self.wt_rm_rc == 0 else "rm failed"
        if "worktree" in argv and "list" in argv:
            if self.wt_rc:
                return self.wt_rc, "", "worktree list down"
            return 0, json.dumps({"result": {"worktrees": self.worktrees}}), ""
        if "list" in argv and "--environment" in argv:
            if self.wsl_rc:
                return self.wsl_rc, "", "wsl list down"
            return 0, json.dumps({"result": {"terminals": self.wsl_terminals}}), ""
        if "list" in argv:
            if self.orca_rc:
                return self.orca_rc, "", "orca down"
            return 0, json.dumps({"result": {"terminals": self.terminals}}), ""
        if "read" in argv:
            if self.screen is None:
                return 1, "", "no"
            return 0, json.dumps({"result": {"terminal": {"tail": self.screen}}}), ""
        return 1, "", "unexpected %r" % argv


def wl_row(dispatch, task, run=None, state="ready", status="dispatched", terminal=None, workspace=None,
           tstate="retained"):
    """A `worker-list --full` row."""
    return {"dispatchId": dispatch, "taskId": task, "runId": run, "workerState": state,
            "dispatchStatus": status, "agentTerminalHandle": terminal, "terminalState": tstate,
            "projection": {"workspace": {"id": workspace}}}


def show_result(state="ready", status="dispatched", dispatched="2026-11-04 06:08:53", completed=None):
    """A `worker-show --full` result."""
    return {"dispatch": {"status": status, "dispatchedAt": dispatched, "completedAt": completed},
            "worker": {"state": state}}
