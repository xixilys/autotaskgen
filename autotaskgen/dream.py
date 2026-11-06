#!/usr/bin/env python3
"""Nightly memory consolidation ("dreaming"): read the last day of events, reports, supervisor findings and agent
transcripts, and keep a small git-versioned memory store of lessons per line. Spec: docs/DREAMING.md.

  dream.py --project dse [--dry-run] [--since ISO] [--line L]

- Transcripts (Codex + Claude, Mac + WSL) are digested by a deterministic script (the extractor below, run locally and
  sent over ssh stdin to WSL so raw files never cross the network). Secrets are redacted in the extractor.
- HOLDOUT RULE (hard): nothing from ~/.dse_holdout/ may reach the store or a model prompt. The extractor never reads
  that directory, drops every session that mentions ".dse_holdout", and drops every digest line containing a term of
  the runtime denylist. The denylist travels only as sha256 hashes of tokens/ngrams; terms are never printed or logged.
- Per line: Claude Sonnet reads the digest (PROGRESS_DREAM_READ_CMD); Claude Opus merges into LESSONS.md
  (PROGRESS_DREAM_MERGE_CMD). Prompt on stdin, answer on stdout. The merge happens in a clone of the store and lands
  by fast-forward only if the store HEAD did not move.
- Never edits STATE.md or anything in the dse-harness repos. --dry-run: extract + digest + stats, no model, no writes.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import decide  # noqa: E402
import progress_core as core  # noqa: E402
import snapshot as snap  # noqa: E402

DEFAULT_HOURS = 24
MAX_HOURS = 72
REPORT_CHARS = 6000
LINE_CAP = 150_000
REPORTS_CAP = 60_000
EVENTS_CAP = 50_000  # newest events kept  # per line per night, newest deliver first; keeps the Sonnet prompt well inside its context
SESSION_HEAD = 10_000
SESSION_TAIL = 30_000
MAX_LESSON_LINES = 120
EXPIRE_DAYS = 7
MODEL_TIMEOUT = 600
SCOPE_WORDS = ["dse-harness", "dse_harness", "autotaskgen"]
HOLDOUT_MARK = ".dse_holdout"
UNASSIGNED = "unassigned"
KINDS = ("pitfall", "rule", "preference", "fact")

READ_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "lessons": {"type": "array", "items": {"type": "object", "properties": {
            "rule": {"type": "string"}, "why": {"type": "string"},
            "kind": {"type": "string", "enum": list(KINDS)}, "confidence": {"type": "number"}},
            "required": ["rule", "why", "kind"]}},
        "state_drift": {"type": "array", "items": {"type": "object", "properties": {
            "state_claim": {"type": "string"}, "reality": {"type": "string"}, "evidence": {"type": "string"}},
            "required": ["state_claim", "reality", "evidence"]}},
        "repeated_failures": {"type": "array", "items": {"type": "object", "properties": {
            "category": {"type": "string"}, "count": {"type": "number"}, "sessions": {"type": "array", "items": {"type": "string"}}}}},
        "memory_loss": {"type": "array", "items": {"type": "object", "properties": {
            "session": {"type": "string"}, "what_was_lost": {"type": "string"}, "cost": {"type": "string"}}}},
    },
    "required": ["lessons", "state_drift", "repeated_failures", "memory_loss"],
}

# ====================================================================== extractor (runs on Mac and, via ssh stdin, WSL)
# Standalone stdlib code: it is exec'd locally and sent verbatim to the remote python3.
EXTRACTOR_SRC = r'''
import glob, hashlib, json, os, re

MAXN = 12
HOLDOUT_MARK = ".dse_holdout"
ORCA_PREAMBLE = "You are working inside Orca"
ORCA_TASK = "=== TASK ==="
# CJK runs are their own tokens; CJK / fullwidth punctuation separates tokens like ASCII punctuation does.
_TOK = re.compile(r"[\u4e00-\u9fff]+|[^\s/\\\"'`,;:()\[\]{}=<>|\u3000-\u303f\uff00-\uffef\u4e00-\u9fff]+")
_SECRETS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "[REDACTED]"),
    (re.compile(r"(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"xox[abprs]-[A-Za-z0-9\-]{8,}"), "[REDACTED]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)(\s*[=:]\s*)\S+"), r"\1\2[REDACTED]"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[REDACTED]"),
]
_B64 = re.compile(r"(?<![\w/.\-])[A-Za-z0-9+_\-]{32,}={0,2}(?![\w/.\-])")


def redact(text):
    for rx, rep in _SECRETS:
        text = rx.sub(rep, text)
    return _B64.sub(lambda m: "[REDACTED]" if re.search(r"\d", m.group(0)) else m.group(0), text)


def toks(s):
    out = []
    for t in _TOK.findall(s.lower()):
        t = t.strip(".-_*#!?@")
        if t:
            out.append(t)
    return out


def hash_gram(tokens):
    return hashlib.sha256(" ".join(tokens).encode("utf-8")).hexdigest()


class Deny(object):
    def __init__(self, hashes, ns):
        self.h = set(hashes or [])
        self.ns = sorted(set(int(n) for n in (ns or []) if 0 < int(n) <= MAXN))

    def hit(self, text):
        if not self.h or not text:
            return False
        t = toks(text)
        for n in self.ns:
            for i in range(len(t) - n + 1):
                if hash_gram(t[i:i + n]) in self.h:
                    return True
        # a name inside a compound id ("evb-<name>-1105", "runs/<name>.json"): every contiguous run of its parts
        for tok in t:
            parts = re.split(r"([-._/])", tok)
            k = (len(parts) + 1) // 2
            if k < 2:
                continue
            for i in range(min(k, 16)):
                for j in range(i, min(k, i + 16)):
                    if i == 0 and j == k - 1:
                        continue
                    c = "".join(parts[2 * i:2 * j + 1]).strip(".-_*#!?@")
                    if len(c) >= 4 and hash_gram([c]) in self.h:
                        return True
        return False


def _text_of(parts):
    if isinstance(parts, str):
        return parts
    out = []
    for p in parts or []:
        if isinstance(p, dict):
            if isinstance(p.get("text"), str):
                out.append(p["text"])
            elif isinstance(p.get("content"), str):
                out.append(p["content"])
    return "\n".join(out)


def _exit_nonzero(out):
    try:
        doc = json.loads(out)
        if isinstance(doc, dict):
            code = (doc.get("metadata") or {}).get("exit_code")
            if isinstance(code, int):
                return code != 0, str(doc.get("output", out))
    except ValueError:
        pass
    m = re.search(r"(?:Exit code:|exited with code|exit code)\s*(-?\d+)", out, re.I)
    return (bool(m) and int(m.group(1)) != 0), out


def _digest(path, source):
    meta = {"cwd": None, "id": None}
    items = []
    state = {"holdout": False, "final": None}

    def add(kind, text):
        if text and text.strip():
            items.append((kind, text.strip()))

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if HOLDOUT_MARK in raw:
                state["holdout"] = True
                break
            try:
                o = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            t = o.get("type")
            if source == "codex":
                p = o.get("payload") if isinstance(o.get("payload"), dict) else {}
                if t == "session_meta":
                    meta["cwd"] = p.get("cwd") or meta["cwd"]
                    meta["id"] = p.get("id") or meta["id"]
                elif t == "compacted":
                    add("compact", "context compacted")
                elif t == "event_msg":
                    if p.get("type") == "context_compacted":
                        add("compact", "context compacted")
                    elif p.get("type") == "task_complete" and p.get("last_agent_message"):
                        state["final"] = str(p["last_agent_message"])
                elif t == "response_item":
                    pt = p.get("type")
                    if pt == "message":
                        text = _text_of(p.get("content"))
                        role = p.get("role")
                        if role == "user" and not text.lstrip().startswith(("<environment_context", "# AGENTS.md", "<user_instructions")):
                            add("user", text)
                        elif role == "assistant":
                            add("assistant", text)
                    elif pt in ("function_call", "custom_tool_call"):
                        args = p.get("arguments") if p.get("arguments") is not None else p.get("input")
                        add("tool", "%s %s" % (p.get("name"), (args if isinstance(args, str) else json.dumps(args))[:200]))
                    elif pt in ("function_call_output", "custom_tool_call_output"):
                        out = p.get("output")
                        out = out if isinstance(out, str) else json.dumps(out)
                        bad, body = _exit_nonzero(out)
                        if bad:
                            add("err", body[:300])
            else:
                if o.get("cwd"):
                    meta["cwd"] = meta["cwd"] or o["cwd"]
                if o.get("sessionId"):
                    meta["id"] = meta["id"] or o["sessionId"]
                if t == "summary":
                    add("compact", str(o.get("summary", ""))[:300] or "context compacted")
                    continue
                msg = o.get("message") if isinstance(o.get("message"), dict) else None
                if t not in ("user", "assistant") or msg is None:
                    continue
                content = msg.get("content")
                if o.get("isCompactSummary"):
                    add("compact", _text_of(content)[:300])
                    continue
                if isinstance(content, str):
                    add("user" if t == "user" else "assistant", content)
                    continue
                for b in content or []:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text":
                        add("user" if t == "user" else "assistant", b.get("text", ""))
                    elif bt == "tool_use":
                        add("tool", "%s %s" % (b.get("name"), json.dumps(b.get("input"), ensure_ascii=False)[:200]))
                    elif bt == "tool_result" and b.get("is_error"):
                        c = b.get("content")
                        add("err", (c if isinstance(c, str) else _text_of(c))[:300])
    return meta, items, state


def _scope_ok(cwd, scopes):
    s = (cwd or "").lower().replace("_", "-")
    return any(w.lower().replace("_", "-") in s for w in scopes)


def extract(P):
    deny = Deny(P.get("hashes"), P.get("ns"))
    scopes = P.get("scopes") or []
    since = float(P.get("since") or 0)
    cap_head, cap_tail = int(P.get("head", 10000)), int(P.get("tail", 30000))
    home = os.path.expanduser("~")
    stats = {"files_seen": 0, "in_window": 0, "in_scope": 0, "excluded_holdout": 0, "dropped_lines": 0, "errors": 0}
    paths = {}
    for pat, src in ((home + "/.codex/sessions/**/*.jsonl", "codex"), (home + "/.claude/projects/*/**/*.jsonl", "claude")):
        for p in glob.glob(pat, recursive=True):
            if HOLDOUT_MARK in p:
                continue
            paths[p] = src
    sessions = []
    for path, src in sorted(paths.items()):
        stats["files_seen"] += 1
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime < since:
            continue
        stats["in_window"] += 1
        try:
            meta, items, st = _digest(path, src)
        except OSError:
            stats["errors"] += 1
            continue
        cwd = meta["cwd"]
        if not cwd and src == "claude":
            cwd = os.path.basename(os.path.dirname(path))
        if not _scope_ok(cwd, scopes):
            continue
        if st["holdout"]:
            stats["excluded_holdout"] += 1
            continue
        stats["in_scope"] += 1
        handles = set()
        lines = []
        raw_chars = 0
        for kind, text in items:
            raw_chars += len(text)
            if deny.hit(text):
                stats["dropped_lines"] += 1
                continue
            handles.update(re.findall(r"term_[A-Za-z0-9-]{6,}", text[:8000]))
            if kind == "user" and ORCA_PREAMBLE in text and ORCA_TASK in text:
                # The Orca dispatch preamble (CLI help, ~4k chars) is identical in every worker; keep only the task.
                text = "[Orca 派单说明已省略]\n" + text[text.index(ORCA_TASK):]
            text = redact(text[:4000])
            lines.append("[%s] %s" % (kind, text))
        final = st["final"]
        if final and not deny.hit(final):
            lines.append("[final] " + redact(final[:4000]))
        body = "\n".join(lines)
        cut = 0
        if len(body) > cap_head + cap_tail:
            cut = len(body) - cap_head - cap_tail
            body = body[:cap_head] + "\n...[cut %d chars]...\n" % cut + body[-cap_tail:]
        sessions.append({"id": meta["id"] or os.path.basename(path), "source": src, "host": P.get("host", "mac"),
                         "cwd": cwd, "path": path, "mtime": mtime, "handles": sorted(handles), "text": body,
                         "cut": cut, "raw_chars": raw_chars})
    return {"sessions": sessions, "stats": stats}
'''

_EX: Dict[str, Any] = {}
exec(compile(EXTRACTOR_SRC, "<dream-extractor>", "exec"), _EX)  # noqa: S102 (our own source)


# ====================================================================== holdout guard

def holdout_dir() -> pathlib.Path:
    return pathlib.Path(os.path.expanduser("~/.dse_holdout"))


def identifier_like(t: str) -> bool:
    """A single token specific enough to deny on its own: letters+digits (>= 5 chars), hyphenated names, or a CJK
    run of >= 5 chars. Plain words, dates and short numbers stay out so ordinary lines are not dropped."""
    if re.fullmatch(r"[\u4e00-\u9fff]+", t):
        return len(t) >= 5
    has_alpha = re.search(r"[^\W\d_]", t) is not None
    if not has_alpha:
        return False
    if re.search(r"\d", t) and len(t) >= 5:
        return True
    return len(t) >= 6 and re.search(r"[-\u2013_.]", t.strip("-\u2013_.")) is not None


class Guard:
    """Holdout denylist as sha256 hashes of lowercased tokens/ngrams. Terms exist only transiently while building."""

    def __init__(self, hashes=None, ns=None):
        self.hashes = sorted(set(hashes or []))
        self.ns = sorted(set(ns or []))
        self._deny = _EX["Deny"](self.hashes, self.ns)

    @classmethod
    def from_holdout(cls, root: Optional[pathlib.Path] = None) -> "Guard":
        root = root or holdout_dir()
        hashes: set = set()
        ns: set = set()
        count = [0]

        def add_one(tk: List[str]) -> None:
            hashes.add(_EX["hash_gram"](tk))
            ns.add(len(tk))
            count[0] += 1

        def add(term: Any) -> None:
            if not isinstance(term, str) or len(term.strip()) < 4 or count[0] > 200_000:
                return
            all_tk = _EX["toks"](term)
            tk = all_tk[:_EX["MAXN"]]
            if not tk or len(" ".join(tk)) < 4:
                return
            add_one(tk)
            # Names buried inside prose lines (model ids, case names, specific CJK terms) must match on their own.
            for t in all_tk:
                if t != " ".join(tk) and identifier_like(t):
                    add_one([t])

        def values(node: Any, idish: bool = False) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    values(v, bool(re.search(r"id|name|case|task|sample|key", str(k), re.I)))
            elif isinstance(node, list):
                for v in node:
                    values(v, idish)
            elif isinstance(node, str) and idish:
                add(node)

        if root.is_dir():
            for p in sorted(root.rglob("*")):
                try:
                    add(p.name)
                    if p.is_file() or p.is_dir():
                        add(p.stem)
                    if not p.is_file() or p.stat().st_size > 2_000_000:
                        continue
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for ln in text.splitlines():
                    add(ln.strip())
                if p.suffix in (".json", ".jsonl"):
                    docs = text.splitlines() if p.suffix == ".jsonl" else [text]
                    for d in docs:
                        try:
                            values(json.loads(d))
                        except ValueError:
                            continue
        return cls(hashes, ns)

    @property
    def active(self) -> bool:
        return bool(self.hashes)

    def hit(self, text: Any) -> bool:
        return isinstance(text, str) and (HOLDOUT_MARK in text or self._deny.hit(text))

    def scrub(self, text: str) -> str:
        """Drop every line that hits the denylist."""
        if not self.active and HOLDOUT_MARK not in text:
            return text
        return "\n".join(ln for ln in text.split("\n") if not self.hit(ln))

    def scrub_obj(self, node: Any) -> Any:
        if isinstance(node, str):
            return "[已剔除]" if self.hit(node) else node
        if isinstance(node, list):
            return [self.scrub_obj(x) for x in node]
        if isinstance(node, dict):
            return {k: self.scrub_obj(v) for k, v in node.items()}
        return node


# ====================================================================== inputs

def window_start(pid: str, now: _dt.datetime, since: Optional[str]) -> _dt.datetime:
    lo = now - _dt.timedelta(hours=MAX_HOURS)
    start = core.parse_iso(since) if since else None
    if start is None and not since:
        last = None
        runs = core.project_dir(pid) / "memory" / "runs"
        for p in sorted(runs.glob("*.json"), reverse=True) if runs.is_dir() else []:
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict) and doc.get("status") == "ok":
                last = core.parse_iso(doc.get("finished_at"))
                break
        start = last or (now - _dt.timedelta(hours=DEFAULT_HOURS))
    if start is None:
        raise core.ProgressError("bad_since", "--since 不是合法时间：%r" % since)
    return max(start, lo)


def extract_local(params: Dict[str, Any]) -> Dict[str, Any]:
    return _EX_RUN(dict(params, host="mac"))


def _EX_RUN(params: Dict[str, Any]) -> Dict[str, Any]:  # noqa: N802
    return _EX["extract"](params)


def extract_remote(params: Dict[str, Any], runner, host: str) -> Dict[str, Any]:
    script = "import json\nPARAMS = json.loads(%s)\n%s\nprint(json.dumps(extract(PARAMS), ensure_ascii=False))\n" % (
        json.dumps(json.dumps(dict(params, host="wsl"))), EXTRACTOR_SRC)
    rc, out, err = runner(snap.ssh_argv(host, "python3 -"), input_text=script, timeout=300)
    doc = snap.parse_json_tail(out) if rc == 0 else None
    if not isinstance(doc, dict) or "sessions" not in doc:
        raise RuntimeError("wsl extraction failed rc=%s %s" % (rc, (err or "")[:120]))
    return doc


def collect_sessions(since: _dt.datetime, guard: Guard, runner, host: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    params = {"since": since.timestamp(), "scopes": SCOPE_WORDS, "hashes": guard.hashes, "ns": guard.ns,
              "head": SESSION_HEAD, "tail": SESSION_TAIL}
    sessions: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {"excluded_holdout": 0, "dropped_lines": 0, "hosts": {}}
    for name, fn in (("mac", lambda: extract_local(params)), ("wsl", lambda: extract_remote(params, runner, host))):
        try:
            doc = fn()
        except Exception as exc:  # noqa: BLE001
            stats["hosts"][name] = {"error": str(exc)[:160]}
            continue
        st = doc.get("stats") or {}
        stats["hosts"][name] = st
        stats["excluded_holdout"] += int(st.get("excluded_holdout") or 0)
        stats["dropped_lines"] += int(st.get("dropped_lines") or 0)
        sessions.extend(doc.get("sessions") or [])
    return sessions, stats


def _wt_path(v: Any) -> str:
    return str(v or "").split("::")[-1].rstrip("/")


def assign_sessions(sessions: List[Dict[str, Any]], project: Dict[str, Any],
                    state: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """cwd matches a node worktree, else a terminal handle in the text matches a node worker / line router."""
    by_wt: List[Tuple[str, str]] = []
    by_handle: Dict[str, str] = {}
    for ln in project.get("lines") or []:
        lid = ln["id"]
        if ln.get("router"):
            by_handle[ln["router"]] = lid
        for n in ((state.get("lines") or {}).get(lid) or {}).get("nodes", {}).values():
            if _wt_path(n.get("worktree")):
                by_wt.append((_wt_path(n["worktree"]), lid))
            if n.get("worker"):
                by_handle[n["worker"]] = lid
    out: Dict[str, List[Dict[str, Any]]] = {}
    for s in sessions:
        cwd = str(s.get("cwd") or "").rstrip("/")
        lid = next((l for wt, l in by_wt if cwd and (cwd == wt or cwd.endswith(wt) or wt.endswith(cwd))), None)
        if lid is None:
            lid = next((by_handle[h] for h in s.get("handles") or [] if h in by_handle), UNASSIGNED)
        out.setdefault(lid, []).append(s)
    return out


def _in_window(at: Optional[str], since: _dt.datetime) -> bool:
    t = core.parse_iso(at)
    return t is not None and t >= since


def read_report(path: str, project: Dict[str, Any], line: Dict[str, Any], runner,
                worktree: str = "") -> Optional[str]:
    if ".." in path.split("/"):
        return None
    if not path.startswith("/") and worktree.startswith("/home/"):
        # Relative report paths are relative to the node's worktree (on WSL); the Mac repo is only a fallback.
        text = read_report(worktree.rstrip("/") + "/" + path, project, line, runner)
        if text:
            return text
    if path.startswith("/home/"):
        import supervisor
        text, _mt, _err = supervisor._read_file(path, runner)  # same ssh path as the supervisor
        return text[:REPORT_CHARS] if text else None
    repo = line.get("repo_path") or project.get("repo_path") or os.environ.get("PROGRESS_REPO") or ""
    p = pathlib.Path(path)
    if not p.is_absolute():
        if not repo:
            return None
        p = pathlib.Path(repo) / path
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:REPORT_CHARS]
    except OSError:
        return None


def supervisor_reports(pid: str, since: _dt.datetime, runner, host: str, local: bool) -> List[Dict[str, Any]]:
    docs: List[Dict[str, Any]] = []
    d = core.supervisor_dir(pid)
    if local or d.is_dir():
        for p in sorted(d.glob("report-*.json")) if d.is_dir() else []:
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            docs.append(doc)
    else:
        sep = "@@DREAM_SEP@@"
        cmd = 'd="${PROGRESS_HOME:-$HOME/.progress}/%s/supervisor"; for f in $(ls -1 "$d"/report-*.json 2>/dev/null | tail -48); do echo %s; cat "$f"; echo; done; true' % (shlex.quote(pid), sep)
        rc, out, _ = runner(snap.ssh_argv(host, cmd), timeout=60)
        for chunk in (out or "").split(sep)[1:] if rc == 0 else []:
            try:
                docs.append(json.loads(chunk.strip()))
            except ValueError:
                continue
    return [x for x in docs if isinstance(x, dict) and _in_window(x.get("generated_at"), since)]


def _dicts_for_line(node: Any, lid: str, out: List[Dict[str, Any]]) -> None:
    if isinstance(node, dict):
        if node.get("line") == lid:
            out.append(node)
        for v in node.values():
            _dicts_for_line(v, lid, out)
    elif isinstance(node, list):
        for v in node:
            _dicts_for_line(v, lid, out)


def followups_for(pid: str, lid: str) -> List[Dict[str, Any]]:
    try:
        rows = list(((snap.load_task_cache().get("supervisor:%s" % pid) or {}).get("followups") or {}).values())
    except Exception:  # noqa: BLE001
        return []
    return [e for e in rows if isinstance(e, dict) and e.get("line") == lid]


def _clip(text: Any, n: int) -> str:
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


def build_bundle(pid: str, lid: str, project: Dict[str, Any], events: List[Dict[str, Any]], since: _dt.datetime,
                 sessions: List[Dict[str, Any]], sup_docs: List[Dict[str, Any]], runner, guard: Guard) -> Dict[str, Any]:
    line = core.line_of(project, lid) or {}
    evs = [e for e in events if e.get("line") == lid and _in_window(e.get("at"), since)]
    ev_lines = []
    for e in sorted(evs, key=lambda x: x.get("at") or ""):
        extra = ""
        if e.get("failures"):
            extra = " failures=" + _clip(e["failures"], 300)
        ev_lines.append("%s %s node=%s role=%s outcome=%s %s%s" % (
            e.get("at"), e.get("kind"), e.get("node"), (e.get("actor") or {}).get("role"), e.get("outcome"),
            e.get("summary") or "", extra))
    total, keep = 0, []
    for ln in reversed(ev_lines):  # newest first until the cap, then back to time order
        total += len(ln) + 1
        if total > EVENTS_CAP:
            keep.append("（更早的 %d 条事件按长度裁掉）" % (len(ev_lines) - len(keep)))
            break
        keep.append(ln)
    ev_lines = list(reversed(keep))
    reports = []
    node_wt: Dict[str, str] = {}
    for e in events:
        wt = (e.get("refs") or {}).get("worktree")
        if e.get("line") == lid and e.get("node") and wt:
            node_wt[str(e["node"])] = _wt_path(wt)
    rep_chars = 0
    for e in sorted(evs, key=lambda x: x.get("at") or "", reverse=True):
        path = (e.get("refs") or {}).get("report") if e.get("kind") == "node_deliver" else None
        if rep_chars >= REPORTS_CAP:
            break
        if path and all(r["path"] != path for r in reports):
            text = read_report(str(path), project, line, runner, node_wt.get(str(e.get("node")), ""))
            if text:
                reports.append({"path": str(path), "text": guard.scrub(text)})
                rep_chars += len(text)
    findings: List[Dict[str, Any]] = []
    for doc in sup_docs:
        _dicts_for_line(doc, lid, findings)
    sup_text = "\n".join(_clip(f, 400) for f in findings[:20])
    fu = "\n".join(_clip(f, 300) for f in followups_for(pid, lid)[:20])
    used, chars, cut_ids = [], 0, []
    for s in sorted(sessions, key=lambda x: x.get("mtime") or 0, reverse=True):
        if chars + len(s["text"]) > LINE_CAP:
            cut_ids.append(s["id"])
            continue
        chars += len(s["text"])
        used.append(s)
    return {"line": lid, "events": [guard.scrub(x) for x in ev_lines], "reports": reports,
            "supervisor": guard.scrub(sup_text), "followups": guard.scrub(fu), "sessions": used,
            "cut_sessions": cut_ids, "chars": chars}


def bundle_empty(b: Dict[str, Any]) -> bool:
    return not (b["events"] or b["sessions"] or b["reports"])


# ====================================================================== B3 read

def read_prompt(b: Dict[str, Any], today: str) -> str:
    parts = [
        "你在为主线 %s 做每晚复盘（日期 %s）。下面是今天的事件、交付报告、巡检员发现和 agent 会话摘要。" % (b["line"], today),
        "只提取会改变下一个 agent 工作方式的东西，输出 JSON：",
        "lessons: [{rule(一句祈使句), why(证据：事件时间 / 报告路径 / 会话 id), kind(pitfall|rule|preference|fact), confidence(0-1)}]；",
        "state_drift: [{state_claim, reality, evidence}]（STATE.md 里看起来过时或与事件日志矛盾的说法）；",
        "repeated_failures: [{category, count, sessions}]；memory_loss: [{session, what_was_lost, cost}]。",
        "不要复述计划；没有证据的不要写；没有就返回空数组。用中文回答。", "",
        "## 事件", "\n".join(b["events"]) or "（无）", "", "## 交付报告"]
    parts += ["### %s\n%s" % (r["path"], r["text"]) for r in b["reports"]] or ["（无）"]
    parts += ["", "## 巡检员发现", b["supervisor"] or "（无）", "", "## 待跟进台账", b["followups"] or "（无）", "", "## 会话摘要"]
    parts += ["### session %s (%s/%s)\n%s" % (s["id"], s["host"], s["source"], s["text"]) for s in b["sessions"]] or ["（无）"]
    return "\n".join(parts)


def _model_argv(env: str, default: List[str]) -> List[str]:
    custom = os.environ.get(env)
    return shlex.split(custom) if custom else default


def call_model(env: str, default: List[str], prompt: str, timeout: int = MODEL_TIMEOUT) -> Optional[Tuple[str, str]]:
    """-> (stdout, ''), one retry; None when both tries fail. Runs in an empty temp dir."""
    argv = _model_argv(env, default)
    with tempfile.TemporaryDirectory() as cwd:
        for _ in range(2):
            rc, out, _err = decide._run(argv, prompt, cwd, timeout)
            if rc == 0 and out.strip():
                return out, ""
    return None


def clean_read(doc: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(doc, dict):
        return None
    out: Dict[str, Any] = {}
    for key in ("lessons", "state_drift", "repeated_failures", "memory_loss"):
        v = doc.get(key)
        out[key] = [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []
    out["lessons"] = [x for x in out["lessons"] if str(x.get("rule") or "").strip() and str(x.get("why") or "").strip()
                      and x.get("kind") in KINDS]
    return out


def run_read(b: Dict[str, Any], today: str, guard: Guard) -> Tuple[Optional[Dict[str, Any]], str]:
    prompt = read_prompt(b, today)
    if guard.hit(prompt):  # belt and braces: a term must never reach a model
        prompt = guard.scrub(prompt)
    default = ["claude", "-p", "--model", "sonnet", "--output-format", "json", "--json-schema",
               json.dumps(READ_SCHEMA, ensure_ascii=False)]
    for _ in range(2):
        got = call_model("PROGRESS_DREAM_READ_CMD", default, prompt)
        if got is None:
            return None, "model_failed"
        doc = clean_read(decide.extract_json(got[0]))
        if doc is not None:
            return guard.scrub_obj(doc), "ok"
    return None, "bad_output"


# ====================================================================== B4 consolidate

_DATE = re.compile(r"\d{4}-\d\d-\d\d")
_RULE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+")
_EVID = re.compile(r"(?:证据|evidence)\s*[:：]\s*\S", re.I)
_EXPIRED = re.compile(r"已过期(?:\s*(\d{4}-\d\d-\d\d))?")


def stamp_expired(text: str, today: str) -> str:
    out = []
    for ln in text.split("\n"):
        m = _EXPIRED.search(ln)
        if m and not m.group(1):
            ln = ln[:m.end()].rstrip() + " " + today + ln[m.end():]
        out.append(ln)
    return "\n".join(out)


def expire(text: str, today: str) -> str:
    """Drop rules marked 已过期 for >= 7 days."""
    now = _dt.date.fromisoformat(today)
    keep = []
    for ln in text.split("\n"):
        m = _EXPIRED.search(ln)
        if m and m.group(1):
            try:
                if (now - _dt.date.fromisoformat(m.group(1))).days >= EXPIRE_DAYS:
                    continue
            except ValueError:
                pass
        keep.append(ln)
    return "\n".join(keep)


def validate_md(text: str) -> Optional[str]:
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or not text.strip():
        return "empty"
    if len(lines) > MAX_LESSON_LINES:
        return "too_long:%d" % len(lines)
    if not any(re.match(r"^#{1,6}\s", ln) for ln in lines):
        return "no_heading"
    for ln in lines:
        if _RULE.match(ln) and not (_EVID.search(ln) and _DATE.search(ln)):
            return "rule_without_evidence_or_date"
    return None


def parse_md_answer(stdout: str) -> str:
    text = stdout
    try:
        doc = json.loads(stdout)
        if isinstance(doc, dict) and isinstance(doc.get("result"), str):
            text = doc["result"]
    except ValueError:
        pass
    text = text.strip()
    m = re.match(r"^```(?:markdown|md)?\s*\n(.*?)\n```\s*$", text, re.S)
    return (m.group(1) if m else text).strip("\n")


def merge_prompt(label: str, old: str, candidates: str, today: str, global_file: bool = False) -> str:
    what = ("GLOBAL.md（跨主线通用经验）" if global_file else "主线 %s 的 LESSONS.md" % label)
    return "\n".join([
        "你在维护 %s。今天是 %s。下面给出当前文件和新的候选经验，请输出完整的新文件（只输出 markdown，不要解释）。" % (what, today),
        "要求：不超过 %d 行；按主题分组（用 # / ## 标题）；每条规则单独一行，格式 `- 规则（证据：<引用>；日期：YYYY-MM-DD）`；"
        "合并重复；被更新证据推翻的规则标成 `- [已过期 %s] 规则（证据：…；日期：…）`，已过期超过 %d 天的删掉；" % (
            MAX_LESSON_LINES, today, EXPIRE_DAYS),
        ("把在 2 条及以上主线都出现的规则提升到这里。" if global_file else "在 2 条及以上主线都出现的规则照常保留，由全局文件另行提升。"),
        "", "## 当前文件", old.strip() or "（空）", "", "## 候选", candidates])


def merge_file(label: str, old: Optional[str], candidates: str, today: str, guard: Guard,
               global_file: bool = False) -> Tuple[Optional[str], str]:
    """-> (new text or None to keep old/skip, status)."""
    old_text = expire(old or "", today) if old else ""
    prompt = merge_prompt(label, old_text, candidates, today, global_file)
    if guard.hit(prompt):
        prompt = guard.scrub(prompt)
    got = call_model("PROGRESS_DREAM_MERGE_CMD", ["claude", "-p", "--model", "opus", "--output-format", "json"], prompt)
    if got is None:
        return None, "model_failed"
    text = expire(stamp_expired(parse_md_answer(got[0]), today), today)
    text = guard.scrub(text)
    err = validate_md(text)
    if err:
        return None, "invalid:" + err
    return text.rstrip("\n") + "\n", "ok"


def render_suggestions(lid: str, drift: List[Dict[str, Any]], today: str) -> str:
    out = ["# STATE 可能过期的地方：%s（%s 夜间整理）" % (lid, today), "",
           "以下只是建议；STATE.md 是否修改由主线自己决定，整理任务从不改它。", ""]
    if not drift:
        out.append("本次没有发现。")
    for i, d in enumerate(drift, 1):
        out += ["%d. STATE 写的：%s" % (i, str(d.get("state_claim", "")).strip()),
                "   实际情况：%s" % str(d.get("reality", "")).strip(),
                "   证据：%s" % str(d.get("evidence", "")).strip()]
    return "\n".join(out) + "\n"


def candidates_text(read: Dict[str, Any], today: str) -> str:
    return json.dumps([dict(x, date=today) for x in read["lessons"]], ensure_ascii=False, indent=1)


# ---- git store

def git(args: List[str], cwd: pathlib.Path, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    p = subprocess.run(["git", "-c", "user.name=progress-dream", "-c", "user.email=dream@localhost",
                        "-c", "commit.gpgsign=false"] + args, cwd=str(cwd), capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        raise RuntimeError("git %s: %s" % (args[0], (p.stderr or p.stdout).strip()[:200]))
    return p


def head_of(repo: pathlib.Path) -> str:
    p = git(["rev-parse", "--verify", "-q", "HEAD"], repo, check=False)
    return p.stdout.strip() if p.returncode == 0 else ""


def ensure_store(store: pathlib.Path) -> None:
    store.mkdir(parents=True, exist_ok=True)
    if not (store / ".git").exists():
        git(["init", "-q"], store)
    if not head_of(store):
        (store / "README.md").write_text("# 夜间整理记忆库\n\n由 dream.py 维护；见 docs/DREAMING.md。\n", encoding="utf-8")
        git(["add", "README.md"], store)
        git(["commit", "-q", "-m", "init memory store"], store)


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read(path: pathlib.Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def apply_to_clone(clone: pathlib.Path, reads: Dict[str, Dict[str, Any]], today: str, guard: Guard,
                   metrics_md: Optional[str], run_doc: Dict[str, Any]) -> Dict[str, Any]:
    """Model merges + file writes inside the clone. Returns per-line outcome {lessons_new, drift, status}."""
    out: Dict[str, Any] = {}
    merged: Dict[str, str] = {}
    for lid, rd in sorted(reads.items()):
        path = clone / lid / "LESSONS.md"
        old = _read(path)
        info = {"lessons_new": 0, "drift": 0, "status": "no_candidates"}
        if rd["lessons"]:
            new, status = merge_file(lid, old, candidates_text(rd, today), today, guard)
            info["status"] = status
            if new is not None:
                _write(path, new)
                info["lessons_new"] = len(rd["lessons"])
        elif old:
            cleaned = expire(old, today)
            if cleaned != old and validate_md(cleaned) is None:
                _write(path, cleaned.rstrip("\n") + "\n")
        if lid != UNASSIGNED:
            _write(clone / lid / "STATE_SUGGESTIONS.md", guard.scrub(render_suggestions(lid, rd["state_drift"], today)))
            info["drift"] = len(rd["state_drift"])
        if (clone / lid / "LESSONS.md").is_file():
            merged[lid] = (clone / lid / "LESSONS.md").read_text(encoding="utf-8")
        out[lid] = info
    with_new = [l for l, i in out.items() if i["lessons_new"]]
    if len(with_new) >= 2:
        gpath = clone / "GLOBAL.md"
        cand = "\n\n".join("### 主线 %s\n%s" % (l, merged[l]) for l in sorted(with_new))
        new, status = merge_file("GLOBAL", _read(gpath), cand, today, guard, global_file=True)
        out["_global"] = status
        if new is not None:
            _write(gpath, new)
    if metrics_md:
        mpath = clone / "METRICS.md"
        _write(mpath, (_read(mpath) or "# 指标\n").rstrip("\n") + "\n\n" + metrics_md.rstrip("\n") + "\n")
    runs = clone / "runs"
    name = "%s.json" % today
    if (runs / name).exists():
        name = "%s-%s.json" % (today, run_doc.get("finished_at", "")[11:16].replace(":", ""))
    run_doc = dict(run_doc, outcomes=out)
    _write(runs / name, json.dumps(guard.scrub_obj(run_doc), ensure_ascii=False, indent=1))
    return out


def consolidate(store: pathlib.Path, reads: Dict[str, Dict[str, Any]], today: str, guard: Guard,
                metrics_fn: Callable[[pathlib.Path], Optional[str]], run_doc: Dict[str, Any],
                before_push: Optional[Callable[[pathlib.Path], None]] = None,
                log: Callable[[str], None] = lambda m: None) -> Dict[str, Any]:
    """Clone, merge, commit, fast-forward the store iff its HEAD still equals the base. One redo, then give up."""
    ensure_store(store)
    for attempt in (1, 2):
        base = head_of(store)
        with tempfile.TemporaryDirectory() as tmp:
            clone = pathlib.Path(tmp) / "clone"
            git(["clone", "-q", str(store), str(clone)], pathlib.Path(tmp))
            outcomes = apply_to_clone(clone, reads, today, guard, metrics_fn(clone), run_doc)
            git(["add", "-A"], clone)
            if not git(["status", "--porcelain"], clone).stdout.strip():
                return {"ok": True, "committed": False, "outcomes": outcomes, "attempts": attempt}
            git(["commit", "-q", "-m", "dream %s" % today], clone)
            if before_push:
                before_push(store)
            if head_of(store) != base:
                log("store HEAD moved during attempt %d" % attempt)
                continue
            try:
                git(["fetch", "-q", str(clone), "HEAD"], store)
                git(["merge", "-q", "--ff-only", "FETCH_HEAD"], store)
            except RuntimeError as exc:
                log("landing failed: %s" % exc)
                return {"ok": False, "error": str(exc), "outcomes": outcomes, "attempts": attempt}
            return {"ok": True, "committed": True, "head": head_of(store), "outcomes": outcomes, "attempts": attempt}
    return {"ok": False, "error": "store_head_moved", "outcomes": {}, "attempts": 2}


def copy_to_wsl(store: pathlib.Path, pid: str, runner, host: str) -> Tuple[bool, str]:
    argv = ["rsync", "-a", "--delete", "--exclude", ".git", "-e", "ssh -o ConnectTimeout=8 -o BatchMode=yes",
            str(store) + "/", "%s:.progress/%s/memory/" % (host, pid)]
    rc, _out, err = runner(argv, timeout=180)
    return rc == 0, (err or "")[:160]


# ====================================================================== B5 metrics

def _norm_fail(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def compute_metrics(project: Dict[str, Any], events: List[Dict[str, Any]], lessons: Dict[str, str],
                    global_text: str, now: _dt.datetime) -> str:
    week_start = now - _dt.timedelta(days=7)
    rows = []
    for ln in project.get("lines") or []:
        lid = ln["id"]
        evs = sorted((e for e in events if e.get("line") == lid), key=lambda e: e.get("at") or "")
        wk = [e for e in evs if _in_window(e.get("at"), week_start)]
        lesson_lines = [l for l in (lessons.get(lid, "") + "\n" + global_text).split("\n") if _RULE.match(l)]
        repeats = 0
        seen: Dict[str, int] = {}
        for e in wk:
            for f in e.get("failures") or [] if e.get("kind") in ("node_deliver", "node_heartbeat", "node_block") else []:
                key = _norm_fail(str(f))
                if len(key) < 6:
                    continue
                for ll in lesson_lines:
                    dates = _DATE.findall(ll)
                    if key in ll.lower() and dates and (e.get("at") or "")[:10] > dates[-1]:
                        seen[key] = seen.get(key, 0) + 1
                        break
        repeats = sum(seen.values())
        lat = []
        for n in (e for e in wk if e.get("kind") == "nudge" and e.get("reason") == "resume"):
            t0 = core.parse_iso(n.get("at"))
            tgt = n.get("target") or (n.get("refs") or {}).get("target") or ln.get("router")
            for e in evs:
                t1 = core.parse_iso(e.get("at"))
                if t0 and t1 and t1 > t0 and e.get("kind") in ("node_plan", "node_deliver") and \
                        (e.get("actor") or {}).get("handle") in (tgt, ln.get("router")):
                    lat.append((t1 - t0).total_seconds() / 60.0)
                    break
        reviews = [e for e in wk if e.get("kind") == "node_review"]
        rejected = [e for e in reviews if e.get("outcome") != "PASS"]
        runs = [e for e in wk if e.get("kind") == "node_deliver" and (e.get("actor") or {}).get("role") != "patrol"]
        rows.append((lid, repeats, ("%.0f" % (sorted(lat)[len(lat) // 2]) if lat else "-"), len(lat),
                     ("%d/%d" % (len(rejected), len(reviews)) if reviews else "-"),
                     ("%.2f" % (len(reviews) / len(runs)) if runs else "-")))
    out = ["## 周报 %s（过去 7 天）" % now.date().isoformat(), "",
           "| 主线 | 教训后重复失败 | 接续耗时中位(分) | 接续次数 | 复核不通过/复核 | 复核/交付 |",
           "|---|---|---|---|---|---|"]
    out += ["| %s | %d | %s | %d | %s | %s |" % r for r in rows]
    return "\n".join(out)


def metrics_for_clone(project, events, now: _dt.datetime):
    def fn(clone: pathlib.Path) -> Optional[str]:
        if now.weekday() != 0:
            return None
        title = "## 周报 %s" % now.date().isoformat()
        if title in (_read(clone / "METRICS.md") or ""):
            return None
        lessons = {ln["id"]: (_read(clone / ln["id"] / "LESSONS.md") or "") for ln in project.get("lines") or []}
        return compute_metrics(project, events, lessons, _read(clone / "GLOBAL.md") or "", now)
    return fn


# ====================================================================== B6 notify

_PROMPT = re.compile(r"^[\s│|]*[❯›]")


def screen_idle(tail: Optional[List[str]]) -> bool:
    if not tail:
        return False
    rows = [t for t in tail if t.strip()][-10:]
    if any(m in "\n".join(rows).lower() for m in snap.WORKING_MARKERS):
        return False
    return any(_PROMPT.match(r) for r in rows[-8:])


def pending_path(pid: str) -> pathlib.Path:
    return core.project_dir(pid) / "dream_pending.json"


def load_pending(pid: str) -> Dict[str, Any]:
    try:
        doc = json.loads(pending_path(pid).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save_pending(pid: str, doc: Dict[str, Any]) -> None:
    p = pending_path(pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def notify(pid: str, project: Dict[str, Any], live: Optional[Dict[str, Any]], outcomes: Dict[str, Any],
           runner) -> Dict[str, Any]:
    pending = load_pending(pid)
    for lid, info in outcomes.items():
        if lid.startswith("_") or lid == UNASSIGNED:
            continue
        if info.get("lessons_new") or info.get("drift"):
            cur = pending.setdefault(lid, {"lessons": 0, "drift": 0})
            cur["lessons"] += int(info.get("lessons_new") or 0)
            cur["drift"] = int(info.get("drift") or 0) if info.get("drift") else cur["drift"]
    sent, waiting = [], []
    for lid, cnt in list(pending.items()):
        line = core.line_of(project, lid)
        handle = (core.core_of(project, (line or {}).get("core")) or {}).get("coordinator")
        if not handle:
            pending.pop(lid, None)
            continue
        if not screen_idle(snap.read_terminal_tail(runner, handle, live, 30)):
            waiting.append(lid)
            continue
        text = "[每晚整理] %s：新增 %d 条经验、%d 处 STATE 可能过期，见 ~/.progress/%s/memory/%s/。STATE 是否修改由你决定。" % (
            lid, cnt["lessons"], cnt["drift"], pid, lid)
        ok, _why, _meta = snap.terminal_send(runner, handle, text, snap.env_for_terminal(handle, live))
        if ok:
            sent.append(lid)
            pending.pop(lid, None)
        else:
            waiting.append(lid)
    try:
        save_pending(pid, pending)
    except OSError:
        pass
    return {"sent": sent, "waiting": waiting}


# ====================================================================== run

def log_err(msg: str) -> None:
    print("dream: %s" % msg, file=sys.stderr)


@contextlib.contextmanager
def run_lock(pid: str):
    path = core.project_dir(pid) / "dream.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        fh.close()


def gather(pid: str, runner, now: _dt.datetime, since_arg: Optional[str], only_line: Optional[str],
           guard: Guard) -> Dict[str, Any]:
    store = snap.Store(pid, runner)
    project, events, live = store.read()
    since = window_start(pid, now, since_arg)
    state = core.fold(project, events)
    sessions, sstats = collect_sessions(since, guard, runner, store.host)
    by_line = assign_sessions(sessions, project, state)
    sup_docs = supervisor_reports(pid, since, runner, store.host, store.local)
    ids = [ln["id"] for ln in project.get("lines") or []] + [UNASSIGNED]
    bundles = {}
    for lid in ids:
        if only_line and lid != only_line:
            continue
        b = build_bundle(pid, lid, project, events, since, by_line.get(lid, []), sup_docs, runner, guard)
        if not bundle_empty(b):
            bundles[lid] = b
    return {"store": store, "project": project, "events": events, "live": live, "since": since,
            "sessions": sessions, "sstats": sstats, "bundles": bundles}


def stats_of(g: Dict[str, Any], now: _dt.datetime, guard: Guard) -> Dict[str, Any]:
    return {"window": {"since": snap.iso(g["since"]), "until": snap.iso(now)},
            "events_total": len(g["events"]), "sessions": len(g["sessions"]),
            "excluded_holdout": g["sstats"]["excluded_holdout"], "dropped_lines": g["sstats"]["dropped_lines"],
            "holdout_guard_active": guard.active, "hosts": g["sstats"]["hosts"],
            "lines": {lid: {"events": len(b["events"]), "reports": len(b["reports"]), "sessions": len(b["sessions"]),
                            "chars": b["chars"], "cut_sessions": b["cut_sessions"]} for lid, b in g["bundles"].items()}}


def run(pid: str, dry_run: bool = False, since: Optional[str] = None, only_line: Optional[str] = None,
        runner=snap.default_runner, now: Optional[_dt.datetime] = None, guard: Optional[Guard] = None,
        before_push: Optional[Callable[[pathlib.Path], None]] = None) -> Dict[str, Any]:
    now = now or _dt.datetime.now().astimezone()
    guard = guard or Guard.from_holdout()
    if dry_run:
        return {"dry_run": True, "stats": stats_of(gather(pid, runner, now, since, only_line, guard), now, guard)}
    with run_lock(pid) as got:
        if not got:
            return {"ok": False, "error": "locked"}
        g = gather(pid, runner, now, since, only_line, guard)
        today = now.date().isoformat()
        stats = stats_of(g, now, guard)
        reads: Dict[str, Dict[str, Any]] = {}
        statuses: Dict[str, str] = {}
        for lid, b in g["bundles"].items():
            rd, status = run_read(b, today, guard)
            statuses[lid] = status
            if rd is not None:
                reads[lid] = rd
        store_dir = core.project_dir(pid) / "memory"
        run_doc = {"date": today, "finished_at": snap.iso(now), "status": "ok", "stats": stats,
                   "read_status": statuses, "reads": reads}
        if not reads:
            return {"ok": False, "error": "nothing_read", "read_status": statuses, "stats": stats}
        res = consolidate(store_dir, reads, today, guard, metrics_for_clone(g["project"], g["events"], now),
                          run_doc, before_push, log_err)
        out: Dict[str, Any] = {"ok": res["ok"], "stats": stats, "read_status": statuses, "consolidate": res}
        if not res["ok"]:
            log_err("consolidation gave up: %s" % res.get("error"))
            return out
        out["wsl_copy"] = dict(zip(("ok", "why"), copy_to_wsl(store_dir, pid, runner, g["store"].host)))
        out["notify"] = notify(pid, g["project"], g["live"], res["outcomes"], runner)
        n_l = sum(i.get("lessons_new", 0) for k, i in res["outcomes"].items() if not k.startswith("_"))
        n_d = sum(i.get("drift", 0) for k, i in res["outcomes"].items() if not k.startswith("_"))
        try:
            g["store"].append({"kind": "observe", "actor": {"role": "patrol", "handle": "dream@mac"},
                               "at": snap.iso(now), "refs": {"key": "dream:%s" % today},
                               "summary": "每晚整理：%d 条主线读完，新增 %d 条经验、%d 处 STATE 疑似过期" % (len(reads), n_l, n_d)})
        except Exception as exc:  # noqa: BLE001
            log_err("observe event failed: %s" % str(exc)[:120])
        return out


def main(argv=None, runner=snap.default_runner, now: Optional[_dt.datetime] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", default=os.environ.get("PROGRESS_PROJECT", "dse"))
    p.add_argument("--dry-run", action="store_true", help="extract + digest + stats only: no model calls, no writes")
    p.add_argument("--since", help="ISO time; default = last successful run, 24 h, at most 72 h back")
    p.add_argument("--line", help="only this line")
    a = p.parse_args(argv)
    try:
        out = run(a.project, a.dry_run, a.since, a.line, runner, now)
    except core.ProgressError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False))
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    if out.get("error") == "locked":
        return 3
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
