#!/usr/bin/env python3
"""Liveness snapshot: router terminals (orca, Mac) and worker processes (ps, WSL) -> live.json.

  python3 snapshot.py [--project dse] [--dry-run] [--no-screen] [--screen-cmd CMD]

- Routers: `orca terminal list --json`; a declared line router matches a terminal handle by
  prefix. `present` = listed and connected. Unreachable orca -> hosts.mac "unknown" and
  present null (never a false "absent").
- Workers: `ssh wsl ps`; processes carrying ORCA_WORKSPACE_ID, one per (workspace, terminal)
  (codex has a node wrapper and a native child). Unreachable WSL -> hosts.wsl "unknown" and
  workers null (never an empty list pretending zero).
- Attribution: worker terminal / workspace vs node refs (node_dispatch refs.terminal /
  refs.worktree) from the folded events; then orca dispatch -> task id == node refs.task; then
  dispatch run objective prefix == line id (line only). Unmatched workers keep line/node null.
- tasks: real dispatch/completion times via `orca orchestration worker-show`, settled ones cached
  in $PROGRESS_CACHE_DIR (default ~/.progress/cache/orca_tasks.json), capped per run.
- screen_flag: deterministic marker check on the router's rendered screen tail; an optional
  external classifier (--screen-cmd, e.g. a cheap-model wrapper) is consulted only when the
  markers find nothing. It never decides anything beyond the flag.
- Store: local save_live when $PROGRESS_HOME/<project>/project.json exists, else
  `ssh wsl python3 ~/opt/progress/progress.py --project <id> live-put`.

Also the shared I/O layer for patrol.py (runner, Store). Standard library, Python 3.9.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import graph  # noqa: E402
import progress_core as core  # noqa: E402

REMOTE_CLI = os.environ.get("PROGRESS_REMOTE_CLI", "~/opt/progress/progress.py")
ORCA_FALLBACK = "/Volumes/remote/Applications/Orca.app/Contents/Resources/bin/orca"
UNREACHABLE = {255, 124, 127}

Runner = Callable[..., Tuple[int, str, str]]


def default_runner(argv: List[str], input_text: Optional[str] = None, timeout: int = 60) -> Tuple[int, str, str]:
    env = None
    if argv and os.path.basename(str(argv[0])) == "orca" and os.environ.get("ORCA_TERMINAL_HANDLE"):
        # Orca attests the calling terminal from this variable; a patrol started by hand inside a coordinator's
        # terminal would otherwise act as that coordinator (worker-start "terminal is attested as ...").
        env = {k: v for k, v in os.environ.items() if k != "ORCA_TERMINAL_HANDLE"}
    try:
        p = subprocess.run(argv, input=input_text, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %ss" % timeout
    except OSError as exc:
        return 127, "", str(exc)


def orca_bin() -> str:
    return os.environ.get("ORCA_BIN") or shutil.which("orca") or ORCA_FALLBACK


def ssh_argv(host: str, cmd: str) -> List[str]:
    return ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", host, cmd]


def remote_host() -> str:
    return os.environ.get("PROGRESS_REMOTE", "wsl")


def parse_json_tail(text: str) -> Optional[Any]:
    """Whole stdout as JSON, else the last line that parses."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    for raw in reversed(text.splitlines()):
        try:
            return json.loads(raw)
        except ValueError:
            continue
    return None


def ms_to_iso(ms: Any) -> Optional[str]:
    try:
        return _dt.datetime.fromtimestamp(float(ms) / 1000.0, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def iso(t: _dt.datetime) -> str:
    return t.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# ---------------------------------------------------------------------- store

SEP = "@@PROGRESS_SEP@@"


class Store:
    """The project's event store: local files, or the WSL store over ssh."""

    def __init__(self, pid: str, runner: Runner = default_runner, host: Optional[str] = None):
        core.project_dir(pid)  # validates the id
        self.pid = pid
        self.runner = runner
        self.host = host or remote_host()
        self.local = (core.home() / pid / "project.json").is_file()

    def read(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        if self.local:
            return core.load_project(self.pid), core.read_events(self.pid), core.load_live(self.pid)
        d = '"${PROGRESS_HOME:-$HOME/.progress}/%s"' % self.pid
        cmd = ('d=%s; if [ -f "$d/project.json" ]; then cat "$d/project.json"; else echo __NOPROJECT__; fi; '
               'echo; echo %s; cat "$d/events.jsonl" 2>/dev/null; echo; echo %s; cat "$d/live.json" 2>/dev/null; true'
               % (d, SEP, SEP))
        rc, out, err = self.runner(ssh_argv(self.host, cmd), timeout=60)
        if rc in UNREACHABLE or SEP not in out:
            raise core.ProgressError("store_unreachable", "读不到 %s 上的进度存储" % self.host,
                                     rc=rc, stderr=(err or "")[:200])
        parts = out.split(SEP)
        if "__NOPROJECT__" in parts[0]:
            raise core.ProgressError("no_project", "项目未注册：%s（%s）" % (self.pid, self.host))
        try:
            project = json.loads(parts[0])
        except ValueError as exc:
            raise core.ProgressError("bad_project", "project.json 不是合法 JSON：%s" % exc)
        events = []
        for raw in (parts[1] if len(parts) > 1 else "").splitlines():
            raw = raw.strip()
            if raw:
                try:
                    events.append(json.loads(raw))
                except ValueError:
                    continue
        live = None
        tail = parts[2].strip() if len(parts) > 2 else ""
        if tail:
            try:
                live = json.loads(tail)
            except ValueError:
                live = None
        return project, events, live

    def _remote(self, sub: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        cmd = "python3 %s --project %s %s" % (REMOTE_CLI, shlex.quote(self.pid), sub)
        rc, out, err = self.runner(ssh_argv(self.host, cmd), input_text=json.dumps(payload, ensure_ascii=False),
                                   timeout=60)
        res = parse_json_tail(out)
        if rc in UNREACHABLE and not isinstance(res, dict):
            raise core.ProgressError("store_unreachable", "写不到 %s 上的进度存储" % self.host,
                                     rc=rc, stderr=(err or "")[:200])
        if not isinstance(res, dict):
            raise core.ProgressError("bad_reply", "远端回执无法解析", rc=rc, stdout=(out or "")[:200])
        if not res.get("ok"):
            raise core.ProgressError(res.get("code") or "rejected", res.get("error") or "远端拒绝",
                                     detail=res.get("detail"))
        return res

    def append(self, event: Dict[str, Any]) -> Dict[str, Any]:
        if self.local:
            return core.append(self.pid, event)
        event = dict(event)
        event.setdefault("id", core.new_id())
        event.setdefault("at", core.now_iso())
        event["project"] = self.pid
        return self._remote("append-raw", event)

    def put_live(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        if self.local:
            core.save_live(self.pid, doc)
            return {"ok": True}
        return self._remote("live-put", doc)

    def put_supervisor(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        if self.local:
            core.save_supervisor(self.pid, doc)
            return {"ok": True}
        return self._remote("supervisor-put", doc)


# ------------------------------------------------------------ terminal send / read

WSL_ENV = os.environ.get("PROGRESS_ORCA_ENV", "wsl")


def env_for_terminal(handle: Optional[str], live: Optional[Dict[str, Any]]) -> Optional[str]:
    """Orca environment selector a terminal handle belongs to: WSL worker terminals need --environment wsl,
    Mac router/coordinator terminals none."""
    for w in (live or {}).get("workers") or []:
        if core._same(w.get("terminal"), handle) and w.get("host") == "wsl":
            return WSL_ENV
    return None


def _orca_error(out: str) -> Tuple[Optional[str], Optional[str]]:
    """-> (error code, retry request id) from an orca --json reply."""
    doc = parse_json_tail(out)
    if not isinstance(doc, dict):
        return None, None
    err = doc.get("error") if isinstance(doc.get("error"), dict) else {}
    found = None
    for src in (err.get("data"), doc.get("result"), err, doc):
        if isinstance(src, dict):
            for k in ("retryRequestId", "retry_request_id", "retryRequest", "requestId"):
                if isinstance(src.get(k), str) and src[k]:
                    found = found or src[k]
    return err.get("code"), found


def terminal_send(runner: Runner, handle: str, text: str, env: Optional[str] = None) -> Tuple[bool, str, Dict[str, Any]]:
    """`orca terminal send --text … --enter --wait-submit 20`. `env` = --environment for the first try; on
    terminal_handle_stale retry once with the other choice (adding the --retry-request id Orca returned, when it
    returned one). Returns (ok, why, meta{env, tries})."""
    tries = []
    order = [env, (None if env else WSL_ENV)]
    retry_id = None
    why = ""
    for i, e in enumerate(order):
        argv = [orca_bin(), "terminal", "send"]
        if e:
            argv += ["--environment", e]
        argv += ["--terminal", handle, "--text", text, "--enter", "--wait-submit", "20"]
        if retry_id:
            argv += ["--retry-request", retry_id]
        argv += ["--json"]
        rc, out, err = runner(argv, timeout=60)
        doc = parse_json_tail(out)
        ok = rc == 0 and not (isinstance(doc, dict) and doc.get("ok") is False)
        tries.append({"env": e, "ok": ok})
        if ok:
            return True, "", {"env": e, "tries": tries}
        code, rid = _orca_error(out)
        why = (code or err or out or "")[:200]
        if code != "terminal_handle_stale" or i == len(order) - 1:
            break
        retry_id = rid
    return False, why, {"env": None, "tries": tries}


def _tail_of(out: str) -> Optional[List[str]]:
    doc = parse_json_tail(out)
    if not isinstance(doc, dict):
        return None
    res = doc.get("result") if isinstance(doc.get("result"), dict) else {}
    term = res.get("terminal") if isinstance(res.get("terminal"), dict) else {}
    tail = term.get("tail")
    return [str(x) for x in tail] if isinstance(tail, list) else None


def read_terminal_tail(runner: Runner, handle: str, live: Optional[Dict[str, Any]] = None,
                       limit: int = 40) -> Optional[List[str]]:
    """`orca terminal read --screen` tail of a terminal handle (WSL worker handles via --environment wsl,
    the other choice as fallback). None when unreadable."""
    first = env_for_terminal(handle, live)
    for e in ([first, (None if first else WSL_ENV)]):
        argv = [orca_bin(), "terminal", "read"] + (["--environment", e] if e else []) + \
               ["--terminal", handle, "--screen", "--limit", str(limit), "--json"]
        rc, out, _ = runner(argv, timeout=30)
        tail = _tail_of(out) if rc == 0 else None
        if tail is not None:
            return tail
    return None


def read_worker_tail(runner: Runner, dispatch: str, limit: int = 40) -> Optional[List[str]]:
    """`orca orchestration worker-read` terminal tail of a dispatch. None when unreadable."""
    rc, out, _ = runner([orca_bin(), "orchestration", "worker-read", "--dispatch", dispatch, "--source", "terminal",
                         "--limit", str(limit), "--json"], timeout=30)
    return _tail_of(out) if rc == 0 else None


# ------------------------------------------------------------------ screen check

SCREEN_MARKERS = [
    ("stream_error", ("stream disconnected", "error running remote compact task", "rate limit",
                      "too many requests", "error sending request", "connection reset",
                      "unexpected status", "stream error")),
    ("waiting_input", ("press enter", "do you want", "(y/n)", "[y/n]", "allow once",
                       "approve this", "continue?")),
]
WORKING_MARKERS = ("esc to interrupt", "working (")
STUCK_SECONDS = 45 * 60


def marker_screen_check(tail: List[str], last_output_at: Optional[str], now: _dt.datetime) -> Optional[str]:
    """Flag from the last rendered lines only (older scrollback may quote old errors)."""
    lines = [l for l in (tail or []) if isinstance(l, str) and l.strip()][-12:]
    text = "\n".join(lines).lower()
    for flag, marks in SCREEN_MARKERS:
        if any(m in text for m in marks):
            return flag
    last = core.parse_iso(last_output_at)
    if last and any(m in text for m in WORKING_MARKERS) and (now - last).total_seconds() > STUCK_SECONDS:
        return "stuck"
    return None


def external_screen_check(cmd: str, runner: Runner) -> Callable[[List[str], Optional[str], _dt.datetime], Optional[str]]:
    """Pluggable classifier: CMD reads the screen on stdin, prints ok|stream_error|stuck|waiting_input."""
    def check(tail, last_output_at, now):
        rc, out, _ = runner(shlex.split(cmd), input_text="\n".join(tail or []), timeout=90)
        word = (out or "").strip().split()[-1:] if rc == 0 else []
        word = word[0].lower() if word else ""
        return word if word in ("stream_error", "stuck", "waiting_input") else None
    return check


# ---------------------------------------------------------------------- collect

_RE_WS = re.compile(r"ORCA_WORKSPACE_ID=['\"]?([^'\"\s]+)")
_RE_TERM = re.compile(r"ORCA_TERMINAL_HANDLE=['\"]?([^'\"\s]+)")
_RE_MODEL = re.compile(r"(?:^|\s)(?:-m|--model)[ =]['\"]?([^'\"\s]+)")
_RE_EFFORT = re.compile(r"model_reasoning_effort=['\"]?([\w.-]+)")
_SKIP_EXE = {"grep", "rg", "ssh", "sh", "bash", "zsh", "ps", "awk", "sed", "cat", "tail"}


def parse_ps(text: str, now: _dt.datetime, host: str = "wsl") -> List[Dict[str, Any]]:
    """`ps -eo pid=,etimes=,args=` lines -> one worker per (workspace, terminal)."""
    best: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for raw in (text or "").splitlines():
        parts = raw.strip().split(None, 2)
        if len(parts) < 3 or "ORCA_WORKSPACE_ID" not in parts[2]:
            continue
        try:
            pid, etimes = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        args = parts[2]
        exe = os.path.basename(args.split(None, 1)[0])
        if exe in _SKIP_EXE:
            continue
        ws = _RE_WS.search(args)
        if not ws:
            continue
        workspace = ws.group(1)
        term = _RE_TERM.search(args)
        terminal = term.group(1) if term else None
        model = _RE_MODEL.search(args)
        effort = _RE_EFFORT.search(args)
        key = (workspace, terminal or "")
        cur = best.get(key)
        if cur is not None and cur["_etimes"] >= etimes:
            continue
        path = workspace.split("::", 1)[-1]
        best[key] = {"host": host, "pid": pid, "workspace": workspace,
                     "worktree_name": os.path.basename(path.rstrip("/")) or None,
                     "terminal": terminal, "model": model.group(1) if model else None,
                     "effort": effort.group(1) if effort else None,
                     "started_at": iso(now - _dt.timedelta(seconds=etimes)),
                     "line": None, "node": None, "_etimes": etimes}
    out = sorted(best.values(), key=lambda w: w["pid"])
    for w in out:
        w.pop("_etimes", None)
    return out


def _ws_path(ws: Optional[str]) -> str:
    return (ws or "").split("::", 1)[-1].rstrip("/")


def ws_match(a: Optional[str], b: Optional[str]) -> bool:
    pa, pb = _ws_path(a), _ws_path(b)
    if not pa or not pb:
        return False
    return pa == pb or pa.endswith("/" + pb) or pb.endswith("/" + pa)


# --------------------------------------------------------------- orca dispatches

FINISHED = {"succeeded", "failed", "cancelled", "canceled", "completed", "stopped", "abandoned"}
INDEX_TERMINAL_STATES = ("active", "retained", "reclaimable", "release_unknown")
PAGE_LIMIT = 100
MAX_PAGES = 20
SHOW_CAP = 60
SHOW_BUDGET_S = 30.0
_RE_TIME = re.compile(r"^(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)(?:\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?$")


def norm_time(v: Any) -> Optional[str]:
    """'2026-11-04 06:08:53' (UTC, no zone), ISO with Z / ms / offset -> 'YYYY-MM-DDTHH:MM:SSZ'."""
    if not isinstance(v, str):
        return None
    m = _RE_TIME.match(v.strip())
    if not m:
        return None
    try:
        t = _dt.datetime.strptime(m.group(1) + "T" + m.group(2), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    tz = m.group(3)
    if tz and tz != "Z":
        sign = -1 if tz[0] == "-" else 1
        digits = tz[1:].replace(":", "")
        t -= sign * _dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def task_state(worker_state: Any, dispatch_status: Any = None) -> Optional[str]:
    ws = worker_state if isinstance(worker_state, str) else None
    ds = dispatch_status if isinstance(dispatch_status, str) else None
    if ws in FINISHED:
        return ws
    if ds in ("completed", "failed"):
        return ds
    return ws or ds


def is_finished(state: Optional[str]) -> bool:
    return state in FINISHED


def _get(d: Any, *path: str) -> Any:
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def fetch_dispatch_index(runner: Runner, orca: str, errors: List[Dict[str, Any]],
                         run_ids: Optional[List[str]] = None) -> Tuple[List[Dict[str, Any]], bool]:
    """Collect dispatches from `orca orchestration worker-list --full`.

    Without --run, orca scopes the list to the Run bound to the calling terminal (a launchd job has
    one), so with run_ids we query every run explicitly (--run <id>, no --terminal-state, cursor
    paging) and keep rows whose terminalState is in INDEX_TERMINAL_STATES or whose worker is not
    finished. With run_ids=None (run-list failed) we fall back to the unscoped per-state query and
    flag the result incomplete when orca reports scope.source == "bound".

    Returns (rows, complete). complete=False when any page failed or was malformed: callers must
    then treat "no dispatch found" as unknown, not as "none"."""
    rows: List[Dict[str, Any]] = []
    seen = set()
    state = {"complete": True}

    def add(w: Dict[str, Any], fallback_ts: Optional[str]) -> None:
        if not isinstance(w, dict) or not w.get("dispatchId") or w["dispatchId"] in seen:
            return
        ts = w.get("terminalState")
        st = task_state(w.get("workerState"), w.get("dispatchStatus"))
        if fallback_ts is None and not (ts in INDEX_TERMINAL_STATES or not is_finished(st)):
            return
        seen.add(w["dispatchId"])
        rows.append({"dispatch": w.get("dispatchId"), "task": w.get("taskId"), "run": w.get("runId"),
                     "state": st, "terminal": w.get("agentTerminalHandle"),
                     "terminal_state": ts or fallback_ts,
                     "workspace": _get(w, "projection", "workspace", "id")})

    def pages(extra: List[str], label: str, fallback_ts: Optional[str]) -> None:
        cursor = None
        for _ in range(MAX_PAGES):
            argv = [orca, "orchestration", "worker-list", "--full", "--json"] + extra + ["--limit", str(PAGE_LIMIT)]
            if cursor:
                argv += ["--cursor", cursor]
            rc, out, err = runner(argv, timeout=60)
            doc = parse_json_tail(out) if rc == 0 else None
            res = doc.get("result") if isinstance(doc, dict) else None
            workers = res.get("workers") if isinstance(res, dict) else None
            if not isinstance(workers, list):
                state["complete"] = False
                errors.append({"host": "mac", "error": "orca worker-list %s 失败或返回格式不对" % label, "rc": rc,
                               "detail": (err or out or "")[:200]})
                return
            if fallback_ts is not None and _get(res, "scope", "source") == "bound":
                state["complete"] = False
                msg = "orca worker-list 只返回了绑定 run 的派单，结果不完整"
                if not any(e.get("error") == msg for e in errors):
                    errors.append({"host": "mac", "error": msg})
            for w in workers:
                add(w, fallback_ts)
            page = res.get("page") if isinstance(res.get("page"), dict) else {}
            cursor = page.get("nextCursor")
            if not (page.get("hasMore") and isinstance(cursor, str) and cursor):
                return
        state["complete"] = False
        errors.append({"host": "mac", "error": "orca worker-list %s 页数超过上限 %d" % (label, MAX_PAGES)})

    if run_ids is None:
        for st in INDEX_TERMINAL_STATES:
            pages(["--terminal-state", st], st, st)
    else:
        for rid in run_ids:
            pages(["--run", rid], "run " + rid, None)
    return rows, state["complete"]


def fetch_runs(runner: Runner, orca: str, errors: List[Dict[str, Any]]) -> Optional[Dict[str, Dict[str, str]]]:
    """run id -> {objective, coordinator}; missing fields become empty strings."""
    rc, out, err = runner([orca, "orchestration", "run-list", "--json", "--full"], timeout=60)
    doc = parse_json_tail(out) if rc == 0 else None
    runs = _get(doc, "result", "runs")
    if not isinstance(runs, list):
        errors.append({"host": "mac", "error": "orca run-list 失败或返回格式不对", "rc": rc,
                       "detail": (err or out or "")[:200]})
        return None
    return {r["id"]: {"objective": str(r.get("objective") or ""),
                      "coordinator": str(r.get("coordinator_handle") or ""),
                      "created_at": str(r.get("created_at") or "")}
            for r in runs if isinstance(r, dict) and r.get("id")}


def fetch_worktrees(runner: Runner, orca: str, errors: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """`orca worktree list` rows (id, path, parentWorktreeId, ...) or None on failure (error recorded)."""
    rc, out, err = runner([orca, "worktree", "list", "--json"], timeout=60)
    doc = parse_json_tail(out) if rc == 0 else None
    wts = _get(doc, "result", "worktrees")
    if not isinstance(wts, list):
        errors.append({"host": "mac", "error": "orca worktree list 失败或返回格式不对，跳过按工作区父子关系找上级", "rc": rc,
                       "detail": (err or out or "")[:200]})
        return None
    return [x for x in wts if isinstance(x, dict) and x.get("id")]


def run_line(objective: Optional[str], line_ids) -> Optional[str]:
    """Objective 'EVB：…' / 'EVB: …' -> 'EVB' when it is a declared line id."""
    m = re.match(r"\s*([^：:]+?)\s*[：:]", objective or "")
    if m and m.group(1) in set(line_ids or []):
        return m.group(1)
    return None



# ------------------------------------------------------------------ router tree

def _trim(text: str, n: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def build_router_tree(project: Optional[Dict[str, Any]], runs: Optional[Dict[str, Dict[str, str]]],
                      rows: Optional[List[Dict[str, Any]]], terminals: Optional[List[Dict[str, Any]]],
                      worktrees: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Router registry + parent edges. Declared: core coordinators, line routers. Discovered ("sub"):
    coordinator of any run that hangs below a declared router (other projects' runs are dropped).

    Parent of a router, first hit wins: line.parent (line or core id) > line.core's coordinator > dispatch edge (its terminal is a
    worker row of the parent's run) > worktree lineage (its terminal's worktree descends from the
    parent's) > default (line -> its core's coordinator). Sub-routers only ever come from the dispatch or
    worktree edge. Declared routers only get parents among declared routers; cycles are cut."""
    runs = runs or {}
    rows = rows or []
    project = project or {}
    terms = [t for t in (terminals or []) if isinstance(t, dict) and t.get("handle")]
    wt_by_id = {w["id"]: w for w in (worktrees or []) if isinstance(w, dict) and w.get("id")}

    nodes: List[Dict[str, Any]] = []

    def find(handle: Optional[str]) -> Optional[Dict[str, Any]]:
        if not handle:
            return None
        return next((n for n in nodes if core._same(n["handle"], handle)), None)

    def term_of(handle: Optional[str]) -> Optional[Dict[str, Any]]:
        return next((t for t in terms if core._same(t["handle"], handle)), None) if handle else None

    def add(handle: str, kind: str, name: str, line: Optional[str], core_id: Optional[str], ref: str) -> Dict[str, Any]:
        n = {"handle": handle, "kind": kind, "name": name, "line": line, "core": core_id, "parent": None,
             "runs": [], "present": None, "last_output_at": None, "edge": "declared", "_ref": ref}
        nodes.append(n)
        return n

    by_core: Dict[str, Dict[str, Any]] = {}
    by_line: Dict[str, Dict[str, Any]] = {}
    for c in project.get("cores") or []:
        if isinstance(c, dict) and c.get("coordinator") and not find(c["coordinator"]):
            by_core[c.get("id")] = add(c["coordinator"], "core", c.get("name") or c.get("id") or "", None,
                                       c.get("id"), c.get("id"))
    lines = [l for l in project.get("lines") or [] if isinstance(l, dict)]
    for l in lines:
        if l.get("router") and not find(l["router"]):
            by_line[l.get("id")] = add(l["router"], "line", l.get("name") or l.get("id") or "", l.get("id"),
                                       l.get("core"), l.get("id"))
    for n in nodes:  # prefer the full terminal handle
        t = term_of(n["handle"])
        if t:
            n["handle"] = t["handle"]
    declared = list(nodes)

    # run ownership
    coord_runs: Dict[str, List[str]] = {}
    for rid, r in sorted(runs.items(), key=lambda kv: (kv[1].get("created_at") or "", kv[0])):
        if r.get("coordinator"):
            coord_runs.setdefault(r["coordinator"], []).append(rid)

    def runs_of(handle: str) -> List[str]:
        out: List[str] = []
        for coord, ids in coord_runs.items():
            if core._same(coord, handle):
                out += [i for i in ids if i not in out]
        return out

    # edge candidates for a router handle: [(edge, parent handle)] in priority order, dispatch/worktree only
    def dispatch_parents(handle: str) -> List[str]:
        out = []
        for r in rows:
            if r.get("terminal") and core._same(r["terminal"], handle):
                coord = (runs.get(r.get("run")) or {}).get("coordinator")
                if coord and not core._same(coord, handle) and coord not in out:
                    out.append(coord)
        return out

    def worktree_parents(handle: str) -> List[str]:
        """Terminals hosted by the nearest ancestor worktrees of the handle's worktree (nearest first)."""
        t = term_of(handle)
        wt = wt_by_id.get(t.get("worktreeId")) if t else None
        out: List[str] = []
        seen = set()
        while wt and wt.get("id") not in seen:
            seen.add(wt["id"])
            pid = wt.get("parentWorktreeId")
            if not pid:
                break
            out += [tt["handle"] for tt in terms if tt.get("worktreeId") == pid and tt["handle"] not in out]
            wt = wt_by_id.get(pid)
        return out

    def has_ancestor(n: Dict[str, Any], target: Dict[str, Any]) -> bool:
        seen = 0
        cur = n
        while cur is not None and seen < 100:
            if cur is target:
                return True
            cur = find(cur["parent"])
            seen += 1
        return False

    # --- declared routers
    def pick(n: Dict[str, Any], cand: List[str], allowed: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for h in cand:
            p = find(h)
            if p is not None and p is not n and p in allowed:
                return p
        return None

    declared_lines = [by_line[l["id"]] for l in lines if l.get("id") in by_line and by_line[l["id"]] in declared]
    for l in lines:
        n = by_line.get(l.get("id"))
        if n is None:
            continue
        ref = l.get("parent")
        par = by_line.get(ref) if ref in by_line else by_core.get(ref)
        edge = "declared"
        if par is None or par is n:
            par, edge = by_core.get(l.get("core")), "default"  # project.json (line.core) beats inference
        if par is None:
            par = pick(n, dispatch_parents(n["handle"]), declared)
            edge = "dispatch"
        if par is None:
            par = pick(n, worktree_parents(n["handle"]), declared)
            edge = "worktree"
        n["parent"], n["edge"] = (par["handle"] if par else None), edge
    for n in declared_lines:  # cycle guard: fall back to the core
        if has_ancestor(find(n["parent"]), n) if n["parent"] else False:
            par = by_core.get(n["core"])
            n["parent"], n["edge"] = (par["handle"] if par else None), "default"

    # --- discovered sub-routers: fixpoint over run coordinators that attach below the tree
    cands = []
    for rid, r in runs.items():
        c = r.get("coordinator")
        if c and not find(c) and not any(core._same(c, x) for x in cands):
            cands.append(c)
    cands.sort()
    changed = True
    while changed:
        changed = False
        for c in list(cands):
            par, edge = None, None
            for e, cl in (("dispatch", dispatch_parents(c)), ("worktree", worktree_parents(c))):
                for h in cl:
                    p = find(h)
                    if p is not None and not core._same(p["handle"], c):
                        par, edge = p, e
                        break
                if par is not None:
                    break
            if par is None:
                continue
            t = term_of(c)
            full = t["handle"] if t else c
            first_obj = next((runs[i].get("objective") for i in runs_of(c) if runs[i].get("objective")), "")
            name = (t or {}).get("paneTitle") or (t or {}).get("title") or _trim(first_obj) or full[:13]
            n = add(full, "sub", _trim(str(name)), None, None, c)
            n["parent"], n["edge"] = par["handle"], edge
            cands.remove(c)
            changed = True

    # --- derived fields
    for n in nodes:
        n["runs"] = runs_of(n["handle"])
        if n["kind"] == "line" and by_line.get(n["_ref"]) is n:
            lrun = next((l.get("run") for l in lines if l.get("id") == n["_ref"]), None)
            if lrun and lrun not in n["runs"]:
                n["runs"].append(lrun)
        t = term_of(n["handle"])
        if terminals is not None:
            n["present"] = bool(t and t.get("connected", True))
            n["last_output_at"] = ms_to_iso(t.get("lastOutputAt")) if t else None
    for n in nodes:
        if n["kind"] == "core":
            continue
        cur, hops = n, 0
        while cur is not None and hops < 100:
            if n["kind"] == "sub":
                if cur["kind"] == "line" and n["line"] is None:
                    n["line"] = cur["line"]
                if cur["kind"] in ("line", "core") and n["core"] is None:
                    n["core"] = cur["core"]
            cur = find(cur["parent"])
            hops += 1
    for n in nodes:
        del n["_ref"]
    return nodes


def find_dispatch(w: Dict[str, Any], rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Dispatch for a live worker: terminal handle first, else workspace; unfinished beats finished."""
    def pick(cands):
        if not cands:
            return None
        live = [r for r in cands if not is_finished(r.get("state"))]
        return (live or cands)[0]
    term = w.get("terminal")
    by_term = [r for r in rows if term and core._same(term, r.get("terminal"))]
    if by_term:
        return pick(by_term)
    return pick([r for r in rows if ws_match(w.get("workspace"), r.get("workspace"))])


def attribute(workers: List[Dict[str, Any]], state: Optional[Dict[str, Any]],
              dispatches: Optional[List[Dict[str, Any]]] = None, dispatches_complete: bool = True,
              runs: Optional[Dict[str, Dict[str, str]]] = None,
              project: Optional[Dict[str, Any]] = None,
              tree: Optional[List[Dict[str, Any]]] = None) -> None:
    """Fill line/node/attribution/task/lingering/router/core.

    Order: terminal handle, worktree path (node refs); then orca dispatch -> taskId == node refs.task
    (node). Then, for line/router only: the dispatch's run -> its coordinator, looked up in the router
    tree; line/core come from that router's nearest line / core ancestor (attribution "run"). If the
    router is unknown, run objective prefix == line id still gives a line. `router` is the coordinator
    handle of the worker's run whenever the dispatch is known, whatever the attribution."""
    plines = [l for l in ((project or {}).get("lines") or []) if isinstance(l, dict)]
    line_core = {l.get("id"): l.get("core") for l in plines}
    tree = tree or []
    cands = []
    task_nodes: Dict[str, Tuple[str, str, str]] = {}
    line_ids = []
    if state:
        line_ids = list((state.get("lines") or {}).keys())
        for lid, ls in (state.get("lines") or {}).items():
            for nid, n in (ls.get("nodes") or {}).items():
                if n.get("worker") or n.get("worktree"):
                    cands.append((lid, nid, n))
                tid = (n.get("refs") or {}).get("task")
                if tid:
                    key = (n.get("dispatched_at") or "", lid, nid)
                    if tid not in task_nodes or key > task_nodes[tid]:
                        task_nodes[tid] = key

    def router_of(rid: Optional[str], coord: Optional[str]) -> Optional[Dict[str, Any]]:
        hit = next((n for n in tree if rid and rid in (n.get("runs") or [])), None)
        return hit or next((n for n in tree if coord and core._same(coord, n.get("handle"))), None)

    for w in workers:
        w.setdefault("attribution", None)
        w["task"] = None
        w["lingering"] = None
        w["core"] = None
        w["router"] = None
        scored = []
        for lid, nid, n in cands:
            by_term = core._same(w.get("terminal"), n.get("worker"))
            by_ws = ws_match(w.get("workspace"), n.get("worktree"))
            if not (by_term or by_ws):
                continue
            scored.append(((1 if by_term else 0), (1 if n.get("status") == "running" else 0),
                           n.get("dispatched_at") or "", lid, nid))
        if scored:
            scored.sort(reverse=True)
            w["line"], w["node"] = scored[0][3], scored[0][4]
            w["attribution"] = "terminal" if scored[0][0] else "worktree"
        if w.get("line"):
            w["core"] = line_core.get(w["line"])
        if dispatches is None:
            continue  # orca index unavailable: task and lingering stay unknown
        d = find_dispatch(w, dispatches)
        if d is None:
            if dispatches_complete:
                w["lingering"] = False
            continue
        w["task"] = {"id": d.get("task"), "run": d.get("run"), "dispatch": d.get("dispatch"),
                     "state": d.get("state"), "dispatched_at": None, "completed_at": None}
        w["lingering"] = is_finished(d.get("state"))
        run = (runs or {}).get(d.get("run")) or {}
        rn = router_of(d.get("run"), run.get("coordinator"))
        if rn:
            w["router"] = rn["handle"]
        if not w["node"] and d.get("task") in task_nodes:
            _, w["line"], w["node"] = task_nodes[d["task"]]
            w["attribution"] = "task"
            w["core"] = line_core.get(w["line"]) or w["core"]
        elif not w["node"]:
            if rn:
                w["line"], w["core"] = rn.get("line"), rn.get("core")
                w["attribution"] = "run"
            elif runs:
                lid = run_line(run.get("objective"), line_ids or [l.get("id") for l in plines])
                if lid:
                    w["line"], w["core"], w["attribution"] = lid, line_core.get(lid), "run"
                    ln = next((n for n in tree if n.get("kind") == "line" and n.get("line") == lid), None)
                    if ln:
                        w["router"] = ln["handle"]


# ----------------------------------------------------------------------- tasks

def cache_path() -> pathlib.Path:
    base = os.environ.get("PROGRESS_CACHE_DIR")
    return (pathlib.Path(base) if base else pathlib.Path.home() / ".progress" / "cache") / "orca_tasks.json"


def load_task_cache() -> Dict[str, Dict[str, Any]]:
    try:
        doc = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in doc.items() if isinstance(v, dict)} if isinstance(doc, dict) else {}


def save_task_cache(cache: Dict[str, Dict[str, Any]]) -> None:
    p = cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(str(tmp), str(p))
    except OSError:
        pass


def _settled(e: Dict[str, Any]) -> bool:
    return is_finished(e.get("state")) and bool(e.get("completed_at"))


def collect_tasks(state: Optional[Dict[str, Any]], workers: Optional[List[Dict[str, Any]]],
                  rows: List[Dict[str, Any]], runner: Runner, orca: str, errors: List[Dict[str, Any]],
                  cap: int = SHOW_CAP, budget_s: float = SHOW_BUDGET_S) -> Dict[str, Dict[str, Any]]:
    """{taskId: {state, dispatched_at, completed_at}} for node refs.task and live workers' tasks.

    Times come from `worker-show` (≈0.2 s each); settled results are cached on disk so a steady-state
    run only asks about unsettled tasks. At most `cap` uncached calls per run; unknown tasks get no entry."""
    import time
    wanted: Dict[str, Dict[str, Any]] = {}  # task -> {dispatch, prio, at}
    by_task = {}
    for r in rows:
        if r.get("task") and r["task"] not in by_task:
            by_task[r["task"]] = r
    for w in workers or []:
        t = w.get("task")
        if t and t.get("id"):
            wanted[t["id"]] = {"dispatch": t.get("dispatch"), "live": True, "at": ""}
    if state:
        for ls in (state.get("lines") or {}).values():
            for n in (ls.get("nodes") or {}).values():
                refs = n.get("refs") or {}
                tid = refs.get("task")
                if not tid:
                    continue
                cur = wanted.setdefault(tid, {"dispatch": None, "live": False, "at": n.get("dispatched_at") or ""})
                if refs.get("dispatch"):
                    cur["dispatch"] = refs["dispatch"]
    cache = load_task_cache()
    out: Dict[str, Dict[str, Any]] = {}
    todo = []
    for tid, info in wanted.items():
        c = cache.get(tid)
        disp = info["dispatch"] or (by_task.get(tid) or {}).get("dispatch") or (c or {}).get("dispatch")
        if c and _settled(c):
            out[tid] = {k: c.get(k) for k in ("state", "dispatched_at", "completed_at")}
            continue
        if not disp:
            if c:  # unsettled cache entry but no dispatch id to refresh with: serve it as is
                out[tid] = {k: c.get(k) for k in ("state", "dispatched_at", "completed_at")}
            continue
        prio = 0 if info["live"] else (1 if not c else 2)
        todo.append((prio, info["at"] or "~", tid, disp))
    todo.sort()
    started = time.monotonic()
    calls = 0
    failed = 0
    for prio, _at, tid, disp in todo:
        idx = by_task.get(tid) or {}
        c = cache.get(tid)
        if calls >= cap or time.monotonic() - started > budget_s:
            if c:
                out[tid] = {k: c.get(k) for k in ("state", "dispatched_at", "completed_at")}
            continue
        calls += 1
        rc, so, se = runner([orca, "orchestration", "worker-show", "--dispatch", disp, "--full", "--json"],
                            timeout=30)
        doc = parse_json_tail(so) if rc == 0 else None
        res = doc.get("result") if isinstance(doc, dict) else None
        if not isinstance(res, dict) or not isinstance(res.get("dispatch"), dict):
            failed += 1
            if failed <= 3:
                errors.append({"host": "mac", "error": "orca worker-show 失败 %s" % disp, "rc": rc,
                               "detail": (se or so or "")[:200]})
            if c:
                out[tid] = {k: c.get(k) for k in ("state", "dispatched_at", "completed_at")}
            elif idx.get("state"):
                out[tid] = {"state": idx["state"], "dispatched_at": None, "completed_at": None}
            continue
        dsp = res["dispatch"]
        ent = {"state": task_state(_get(res, "worker", "state"), dsp.get("status")) or idx.get("state"),
               "dispatched_at": norm_time(dsp.get("dispatchedAt")),
               "completed_at": norm_time(dsp.get("completedAt"))}
        out[tid] = ent
        cache[tid] = dict(ent, dispatch=disp)
    if failed > 3:
        errors.append({"host": "mac", "error": "orca worker-show 另有 %d 次失败" % (failed - 3)})
    skipped = len(todo) - calls
    if skipped > 0:
        errors.append({"host": "mac", "error": "worker-show 本轮上限 %d，另有 %d 个任务留待下轮" % (cap, skipped)})
    if calls:
        save_task_cache(cache)
    return out


def apply_task_times(workers: Optional[List[Dict[str, Any]]], tasks: Dict[str, Dict[str, Any]]) -> None:
    """Copy times / fresher state from `tasks` onto worker.task and recompute lingering."""
    for w in workers or []:
        t = w.get("task")
        if not t:
            continue
        e = tasks.get(t.get("id"))
        if e:
            t["state"] = e.get("state") or t.get("state")
            t["dispatched_at"], t["completed_at"] = e.get("dispatched_at"), e.get("completed_at")
        w["lingering"] = is_finished(t.get("state")) if t.get("state") else None


# ------------------------------------------------------- worker screens (stalled-worker detection)

WORKER_ERROR_MARKERS = tuple(m for _f, ms_ in SCREEN_MARKERS if _f == "stream_error" for m in ms_) + (
    "bad request", "invalid_request_error", "\"error\":{", "traceback (most recent call last)")
WORKER_TAIL_LINES = 40
WORKER_SCREEN_CAP = 30
WORKER_MIN_AGE_S = 10 * 60
WORKER_IDLE_S = 10 * 60
_EXITED = ("exited", "terminated", "stopped", "closed", "released", "dead", "gone")


# Transient = retrying is the right answer: API errors, network failures, capacity limits.
TRANSIENT_STRONG = ("stream disconnected", "error running remote compact task", "rate limit", "too many requests",
                    "error sending request", "connection reset", "unexpected status", "stream error",
                    "bad request", "invalid_request_error", "\"error\":{", "{\"error\"", "unexpected status", "overloaded", "connection closed",
                    "connection refused", "service unavailable", "temporarily unavailable",
                    "api error", "held this request", "without routing", "gateway")
# broad words: only count when the same line also looks like an error line
TRANSIENT_WEAK = ("timeout", "timed out", "capacity", "quota", "insufficient", "529", "503", "500", "502", "504")
_RE_WEAK = re.compile(r"(?<![\w.])(?:%s)(?![\w.])" % "|".join(re.escape(m) for m in TRANSIENT_WEAK))
_ERRISH = ("error", "\u25a0", "\u26a0", "failed", "exceeded", "stream", "status", "http", "api")


PROMPT_MARKS = ("\u203a", "\u276f")   # Codex input box, Claude Code input box


def screen_signals(tail: List[str]) -> Dict[str, Any]:
    """From a terminal tail: {transient, prompt, working, line}. `prompt` = the input box (a line starting
    with the Codex "\u203a" or Claude Code "\u276f" marker) is among the last lines; `transient` = a retryable error in the last 12
    lines while nothing is working."""
    lines = [l.rstrip() for l in (tail or []) if isinstance(l, str) and l.strip()]
    low_all = "\n".join(lines).lower()
    working = any(m in low_all for m in WORKING_MARKERS)
    prompt = any(l.strip().startswith(PROMPT_MARKS) for l in lines[-8:])
    hit = ""
    if not working:
        for l in reversed(lines[-12:]):
            ll = l.lower()
            if any(m in ll for m in TRANSIENT_STRONG) or (_RE_WEAK.search(ll) and any(e in ll for e in _ERRISH)):
                hit = l.strip()[:160]
                break
    return {"transient": bool(hit), "prompt": prompt, "working": working, "line": hit}


_RE_CTX_CLAUDE = re.compile(r"context[ :]+[\u2580-\u259f\u25a0-\u25ff\u00b7\-=\[\] ]*?(\d{1,3})\s*%", re.I)
_RE_CTX_CODEX = re.compile(r"(\d{1,3})\s*%\s*context\s+left", re.I)


def context_signals(tail: List[str]) -> Dict[str, Any]:
    """From a terminal tail: {compact, used}. `compact` = a line mentions compact/Compacting/压缩 (compaction ran or is
    running); `used` = share of the context window in use (0-100) from Claude's "Context ███░░░ NN%" gauge
    (NN is used) or Codex's "NN% context left" (used = 100 - NN); None when no gauge is on screen."""
    lines = [l for l in (tail or []) if isinstance(l, str)]
    low = "\n".join(lines).lower()
    compact = "compact" in low or "\u538b\u7f29" in low
    used = None
    for l in reversed(lines):
        m = _RE_CTX_CODEX.search(l)
        if m and int(m.group(1)) <= 100:
            used = 100 - int(m.group(1))
            break
        m = _RE_CTX_CLAUDE.search(l)
        if m and int(m.group(1)) <= 100:
            used = int(m.group(1))
            break
    return {"compact": compact, "used": used}


def _shown_line(lines: List[str]) -> str:
    """Last non-empty line that is not the input prompt / footer chrome, trimmed to 160 chars."""
    pick = ""
    for l in reversed(lines):
        t = l.strip()
        if not t or t.startswith("\u203a") or "? for shortcuts" in t or " \u00b7 ~/" in t:
            continue
        pick = t
        break
    return (pick or (lines[-1].strip() if lines else ""))[:160]


def classify_worker_screen(tail: List[str], status: Dict[str, Any], term_status: Any, prev: Dict[str, Any],
                           now: _dt.datetime) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """-> (w["screen"], new cache entry). Flags: exited | error | idle | None; `since` = when it began."""
    lines = [l.rstrip() for l in (tail or []) if isinstance(l, str) and l.strip()][-WORKER_TAIL_LINES:]
    h = hashlib.sha1("\n".join(lines).encode("utf-8", "replace")).hexdigest()[:16]
    stamp = iso(now)
    same = prev.get("hash") == h and prev.get("hash_since")
    hash_since = prev["hash_since"] if same else stamp
    low = "\n".join(lines[-12:]).lower()
    working = any(m in "\n".join(lines).lower() for m in WORKING_MARKERS)
    live = (status or {}).get("liveness")
    flag = None
    since = None
    if (live and live != "live") or str(term_status or "").lower() in _EXITED \
            or str((status or {}).get("terminal") or "").lower() in _EXITED:
        flag = "exited"
    elif not working and any(m in low for m in WORKER_ERROR_MARKERS):
        flag = "error"
    elif not working and same:
        a, b = core.parse_iso(hash_since), now
        if a and (b - a).total_seconds() >= WORKER_IDLE_S:
            flag = "idle"
    if flag == "idle":
        since = hash_since
    elif flag:
        since = prev.get("flag_since") if prev.get("flag") == flag and prev.get("flag_since") else stamp
        if flag == "error" and same:
            since = min(since, hash_since)
    screen = {"flag": flag, "line": _shown_line(lines) if flag else None, "checked_at": stamp}
    if flag:
        screen["since"] = since
    if flag == "error":
        sig = screen_signals(lines)
        screen["transient"] = sig["transient"]
        screen["prompt"] = sig["prompt"]
    return screen, {"hash": h, "hash_since": hash_since, "flag": flag, "flag_since": since, "checked_at": stamp}


def collect_worker_screens(workers: Optional[List[Dict[str, Any]]], runner: Runner, orca: str,
                           errors: List[Dict[str, Any]], now: _dt.datetime,
                           cap: int = WORKER_SCREEN_CAP) -> Dict[str, int]:
    """Read the screen of every live, unfinished worker whose dispatch is known (orca worker-read, which
    reaches WSL terminals from the Mac) and set w["screen"]. Never raises."""
    cands = []
    for w in workers or []:
        t = w.get("task") or {}
        if not t.get("dispatch") or is_finished(t.get("state")):
            continue
        s = core.parse_iso(w.get("started_at"))
        if s is not None and (now - s).total_seconds() < WORKER_MIN_AGE_S:
            continue
        cands.append(w)
    if not cands:
        return {"tried": 0, "failed": 0}
    cache = load_task_cache()
    failed = 0
    tried = len(cands[:cap])
    for w in cands[:cap]:
        disp = w["task"]["dispatch"]
        key = "screen:" + disp
        try:
            rc, so, se = runner([orca, "orchestration", "worker-read", "--dispatch", disp, "--source", "terminal",
                                 "--limit", str(WORKER_TAIL_LINES), "--json"], timeout=30)
            doc = parse_json_tail(so) if rc == 0 else None
            res = doc.get("result") if isinstance(doc, dict) else None
            if not isinstance(res, dict):
                raise ValueError("rc=%s %s" % (rc, (se or so or "")[:120]))
            term = res.get("terminal") if isinstance(res.get("terminal"), dict) else {}
            tail = term.get("tail")
            status = res.get("status") if isinstance(res.get("status"), dict) else {}
            if not isinstance(tail, list):
                if (status.get("liveness") or "live") == "live":
                    raise ValueError("worker-read 没有终端内容")
                tail = []
            w["screen"], cache[key] = classify_worker_screen(
                tail, status, term.get("status"), cache.get(key) or {}, now)
        except Exception as exc:  # noqa: BLE001 - one bad worker must not sink the snapshot
            failed += 1
            if failed <= 3:
                errors.append({"host": "mac", "error": "orca worker-read 失败 %s" % disp, "detail": str(exc)[:200]})
    if failed > 3:
        errors.append({"host": "mac", "error": "orca worker-read 另有 %d 次失败" % (failed - 3)})
    if len(cands) > cap:
        errors.append({"host": "mac", "error": "worker-read 本轮上限 %d，另有 %d 个 worker 留待下轮" % (cap, len(cands) - cap)})
    cutoff = iso(now - _dt.timedelta(days=1))
    for k in [k for k, v in cache.items() if k.startswith("screen:") and (v.get("checked_at") or "") < cutoff]:
        del cache[k]
    save_task_cache(cache)
    return {"tried": tried, "failed": failed}


WSL_TITLE_SPINNER = (0x2800, 0x28FF)


def title_busy(title: Any) -> bool:
    """A terminal title starting with a braille spinner glyph (U+2800-U+28FF) means the agent is working."""
    return isinstance(title, str) and bool(title) and WSL_TITLE_SPINNER[0] <= ord(title[0]) <= WSL_TITLE_SPINNER[1]


def collect_worker_activity(workers: Optional[List[Dict[str, Any]]], runner: Runner, orca: str,
                            errors: List[Dict[str, Any]]) -> None:
    """`orca terminal list --environment wsl`: set last_output_at / title / busy on every worker whose terminal
    is listed (None otherwise). A failed call is recorded in errors. Never raises."""
    ws = workers or []
    for w in ws:
        w["last_output_at"], w["title"], w["busy"] = None, None, None
    try:
        rc, out, err = runner([orca, "terminal", "list", "--environment", WSL_ENV, "--json"], timeout=60)
        doc = parse_json_tail(out) if rc == 0 else None
        terms = (doc.get("result") or {}).get("terminals") if isinstance(doc, dict) else None
        if not isinstance(terms, list):
            raise ValueError("rc=%s %s" % (rc, (err or out or "")[:160]))
    except Exception as exc:  # noqa: BLE001
        errors.append({"host": "wsl", "error": "orca terminal list --environment wsl 失败", "detail": str(exc)[:200]})
        return
    for w in ws:
        if not w.get("terminal"):
            continue
        t = next((t for t in terms if isinstance(t, dict) and core._same(t.get("handle"), w["terminal"])), None)
        if t is None:
            continue
        title = t.get("title")
        w["last_output_at"] = ms_to_iso(t.get("lastOutputAt"))
        w["title"] = str(title)[:80] if title else None
        w["busy"] = title_busy(title)


DISK_CRIT_GB = 15
DISK_LOW_GB = 50
# per-host (low, critical) overrides; 11-06 user: the Mac's disk is small by nature, warn only below 10 G.
# Env PROGRESS_DISK_GB="mac=10:5,wsl=50:15" replaces this table.
DISK_HOST_GB = {"mac": (10, 5)}


def disk_thresholds(host: Optional[str] = None) -> Tuple[float, float]:
    """-> (low_gb, critical_gb) for host."""
    table = dict(DISK_HOST_GB)
    spec = os.environ.get("PROGRESS_DISK_GB")
    if spec:
        table = {}
        for part in spec.split(","):
            h, _, v = part.partition("=")
            lo, _, cr = v.partition(":")
            try:
                table[h.strip()] = (float(lo), float(cr or lo))
            except ValueError:
                continue
    return table.get(host or "", (DISK_LOW_GB, DISK_CRIT_GB))
_RE_DF = re.compile(r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)%\s+(/\S*)\s*$")
_RE_MEM = re.compile(r"MemAvailable:\s*(\d+)\s*kB")
RES_CMD = 'df -Pk / "$HOME"; grep MemAvailable /proc/meminfo'


def disk_level(avail_gb: float, host: Optional[str] = None) -> str:
    low, crit = disk_thresholds(host)
    return "critical" if avail_gb < crit else "low" if avail_gb < low else "ok"


def parse_resources(text: str, now: _dt.datetime, host: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """`df -Pk` rows (+ optional MemAvailable line) -> {disks, mem_avail_gb, checked_at}; None if no disk row parsed."""
    disks: List[Dict[str, Any]] = []
    seen = set()
    mem = None
    for ln in (text or "").splitlines():
        m = _RE_MEM.search(ln)
        if m:
            mem = round(int(m.group(1)) / 1048576.0, 1)
            continue
        m = _RE_DF.search(ln.strip())
        if not m or m.group(5) in seen:
            continue
        seen.add(m.group(5))
        avail = int(m.group(3)) / 1048576.0
        disks.append({"mount": m.group(5), "avail_gb": round(avail, 1), "used_pct": int(m.group(4)),
                      "level": disk_level(avail, host)})
    if not disks:
        return None
    return {"disks": disks, "mem_avail_gb": mem, "checked_at": iso(now)}


def collect_resources(runner: Runner, host: str, now: _dt.datetime, errors: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    jobs = [(host, ssh_argv(host, RES_CMD)), ("mac", ["df", "-Pk", os.path.expanduser("~")])]
    for h, argv in jobs:
        rc, so, err = runner(argv, timeout=30)
        res = parse_resources(so, now, h) if rc == 0 else None
        if res is None:
            errors.append({"host": h, "error": "磁盘/内存检查失败", "rc": rc, "detail": (err or so or "")[:200]})
        else:
            out[h] = res
    return out


def collect(project: Dict[str, Any], state: Optional[Dict[str, Any]], runner: Runner = default_runner,
            host: Optional[str] = None, now: Optional[_dt.datetime] = None, screen: bool = True,
            screen_check: Optional[Callable] = None) -> Dict[str, Any]:
    now = now or utcnow()
    host = host or remote_host()
    orca = orca_bin()
    errors: List[Dict[str, Any]] = []
    hosts = {"mac": "ok", host: "ok", "ib-agent": "unknown"}

    terminals = None
    rc, out, err = runner([orca, "terminal", "list", "--json"], timeout=60)
    doc = parse_json_tail(out) if rc == 0 else None
    if isinstance(doc, dict):
        terms = (doc.get("result") or {}).get("terminals")
        if isinstance(terms, list):
            terminals = [t for t in terms if isinstance(t, dict) and t.get("handle")]
    if terminals is None:
        hosts["mac"] = "unknown"
        errors.append({"host": "mac", "error": "orca terminal list 失败", "rc": rc,
                       "detail": (err or out or "")[:200]})

    runs = fetch_runs(runner, orca, errors)
    routers: Dict[str, Any] = {}
    coords: Dict[str, Any] = {}
    rcache = load_task_cache() if screen else {}
    screen_reads = {"tried": 0, "failed": 0}
    # lines routers, then core coordinators (live.coordinators): both get the same screen read
    entries = [(ln["id"], ln.get("router"), False, ln.get("run")) for ln in project.get("lines") or []]
    entries += [(str(c.get("id")), c.get("coordinator"), True, None) for c in project.get("cores") or []]
    for rkey, handle, is_coord, run_id in entries:
        if not handle:
            continue
        declared = handle
        rebound = False
        if terminals is not None and run_id and not is_coord:
            def _live(h):
                return next((t for t in terminals if core._same(t.get("handle"), h) and t.get("connected", True)), None)
            new = ((runs or {}).get(run_id) or {}).get("coordinator")
            if not _live(handle) and new and not core._same(new, handle) and _live(new):
                handle, rebound = _live(new)["handle"], True
        ent: Dict[str, Any] = {"handle": handle, "present": None, "last_output_at": None,
                               "screen_flag": None, "screen_checked": False}
        if is_coord:
            ent["core"] = rkey
        if rebound:
            ent["rebound_from"], ent["rebound_via"] = declared, "run"
        if terminals is not None:
            t = next((t for t in terminals if core._same(t.get("handle"), handle)), None)
            ent["present"] = bool(t and t.get("connected", True))
            if t:
                ent["handle"] = t["handle"]
                ent["last_output_at"] = ms_to_iso(t.get("lastOutputAt"))
            if t and ent["present"] and screen:
                rc2, out2, _ = runner([orca, "terminal", "read", "--terminal", t["handle"], "--screen",
                                       "--limit", "40", "--json"], timeout=30)
                rd = parse_json_tail(out2) if rc2 == 0 else None
                tail = None
                if isinstance(rd, dict):
                    tail = ((rd.get("result") or {}).get("terminal") or {}).get("tail")
                screen_reads["tried"] += 1
                if not isinstance(tail, list):
                    screen_reads["failed"] += 1
                if not isinstance(tail, list) and t.get("preview"):
                    tail = [str(t["preview"])]
                if isinstance(tail, list):
                    ent["screen_checked"] = True
                    flag = marker_screen_check(tail, ent["last_output_at"], now)
                    if flag is None and screen_check is not None:
                        flag = screen_check(tail, ent["last_output_at"], now)
                    ent["screen_flag"] = flag
                    sig = screen_signals(tail)
                    ck = "rscreen:" + t["handle"]
                    prev = rcache.get(ck) or {}
                    since = prev.get("since") if sig["transient"] and prev.get("transient") and prev.get("since") else iso(now)
                    ent["screen"] = {"transient": sig["transient"], "prompt": sig["prompt"],
                                     "line": sig["line"] or None, "checked_at": iso(now)}
                    ctx = context_signals(tail)
                    ent["screen"]["compact"] = ctx["compact"]
                    if ctx["used"] is not None:
                        ent["screen"]["ctx_used"] = ctx["used"]
                    if sig["transient"]:
                        ent["screen"]["since"] = since
                    rcache[ck] = {"transient": sig["transient"], "since": since if sig["transient"] else None,
                                  "checked_at": iso(now)}
        (coords if is_coord else routers)[rkey] = ent
    if screen and any(k.startswith("rscreen:") for k in rcache):
        cut = iso(now - _dt.timedelta(days=1))
        for k in [k for k, v in rcache.items() if k.startswith("rscreen:") and (v.get("checked_at") or "") < cut]:
            del rcache[k]
        save_task_cache(rcache)

    workers: Optional[List[Dict[str, Any]]] = None
    rc, out, err = runner(ssh_argv(host, "ps -ww -eo pid=,etimes=,args="), timeout=60)
    ps_ok = rc == 0
    if not ps_ok:
        hosts[host] = "unknown"
        errors.append({"host": host, "error": "ps 失败或主机不可达", "rc": rc, "detail": (err or "")[:200]})

    rows, complete = fetch_dispatch_index(runner, orca, errors, None if runs is None else list(runs))
    index_ok = complete or bool(rows)
    worktrees = fetch_worktrees(runner, orca, errors)
    tree = build_router_tree(project, runs, rows, terminals, worktrees)
    if ps_ok:
        workers = parse_ps(out, now, host)
        attribute(workers, state, rows if index_ok else None, complete, runs, project, tree)
    tasks = collect_tasks(state, workers, rows, runner, orca, errors)
    apply_task_times(workers, tasks)
    wr = collect_worker_screens(workers, runner, orca, errors, now)
    screen_reads["tried"] += wr["tried"]
    screen_reads["failed"] += wr["failed"]
    collect_worker_activity(workers, runner, orca, errors)
    resources = collect_resources(runner, host, now, errors)
    # dispatches Orca still counts as holding a reclaimable terminal; None when the index is incomplete
    reclaimable = sorted(r["dispatch"] for r in rows if r.get("terminal_state") == "reclaimable") if complete else None

    metrics = scheduling_metrics(project, state, now)
    return {"schema": "progress-live/v1", "generated_at": iso(now), "source": "mac-snapshot",
            "hosts": hosts, "routers": routers, "coordinators": coords, "router_tree": tree, "workers": workers, "tasks": tasks, "resources": resources, "metrics": metrics,
            "reclaimable": reclaimable, "screen_reads": screen_reads, "errors": errors}


def scheduling_metrics(project: Optional[Dict[str, Any]], state: Optional[Dict[str, Any]],
                       now: _dt.datetime) -> Dict[str, Any]:
    """FRAMEWORK §7 per-line scheduling metrics (graph.line_metrics) for open lines; {} without state."""
    if not isinstance(state, dict):
        return {}
    exp = dict(graph.DEFAULT_WEIGHT, **(((project or {}).get("defaults") or {}).get("expected_minutes") or {}))
    return {lid: graph.line_metrics(ls.get("nodes") or {}, now, exp)
            for lid, ls in sorted((state.get("lines") or {}).items()) if ls.get("open", True)}


def summary(doc: Dict[str, Any]) -> Dict[str, Any]:
    routers = doc.get("routers") or {}
    workers = doc.get("workers")
    res = doc.get("resources") or {}
    return {
        "hosts": doc.get("hosts"),
        "routers_declared": len(routers),
        "routers_present": sum(1 for r in routers.values() if r.get("present") is True),
        "routers_absent": sorted(k for k, r in routers.items() if r.get("present") is False),
        "routers_rebound": sorted(k for k, r in routers.items() if r.get("rebound_from")),
        "resources": len(res),
        "disk_min_avail_gb": min((d["avail_gb"] for r in res.values() for d in r.get("disks") or []), default=None),
        "screen_flags": {k: r["screen_flag"] for k, r in routers.items() if r.get("screen_flag")},
        "workers_found": None if workers is None else len(workers),
        "workers_attributed": None if workers is None else sum(1 for w in workers if w.get("node")),
        "attribution": None if workers is None else {
            k: sum(1 for w in workers if w.get("attribution") == k) for k in ("terminal", "worktree", "task", "run")},
        "router_tree": len(doc.get("router_tree") or []),
        "workers_lingering": None if workers is None else sum(1 for w in workers if w.get("lingering") is True),
        "tasks": len(doc.get("tasks") or {}),
        "errors": len(doc.get("errors") or []),
    }


def main(argv=None, runner: Runner = default_runner) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", default=os.environ.get("PROGRESS_PROJECT", "dse"))
    p.add_argument("--dry-run", action="store_true", help="print the document, do not write it")
    p.add_argument("--no-screen", action="store_true", help="skip reading router screens")
    p.add_argument("--screen-cmd", help="optional classifier command (stdin = screen tail)")
    a = p.parse_args(argv)
    try:
        store = Store(a.project, runner)
    except core.ProgressError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        return 2
    state = None
    try:
        project, events, _live = store.read()
        state = core.fold(project, events)
    except core.ProgressError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        return 2
    check = external_screen_check(a.screen_cmd, runner) if a.screen_cmd else None
    doc = collect(project, state, runner, screen=not a.no_screen, screen_check=check)
    if a.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "summary": summary(doc), "live": doc},
                         ensure_ascii=False, indent=1))
        return 0
    try:
        store.put_live(doc)
    except core.ProgressError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, "summary": summary(doc)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
