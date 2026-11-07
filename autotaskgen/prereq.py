"""Mechanical prerequisite check before a node is dispatched (router rules v2, R1, 11-07).

A node spec has a section whose heading contains 「前置条件」. Every list item in it names at least one
input in backticks, in one of three forms:

  `node:<id>` / `node:<LINE>/<id>`   an upstream node's delivery: delivered PASS and not reviewed FAIL (an old
                                     PARTIAL delivery does not count; cite its file or sha instead); a superseded
                                     node counts through its replacement
  `sha:<7-40 hex>`                   a commit that exists in the repo
  `file:<ref>@<path>`                a file at a branch / tag / sha; `file:<path>` means the spec's own branch

A section whose only item is 「无」 means the node needs nothing. An item without any of these tokens cannot be
checked mechanically and is a format error, as is a missing section.

check() returns {"ok", "items", "missing", "format", "unknown"}:
- missing: the inputs that are known to be absent -> do not dispatch, write node_block blocker=dep naming them;
- format: the spec cannot be checked (no section / an item without a token);
- unknown: no source could answer (git unreadable everywhere) -> do not dispatch, try again next round.

Git questions go to the local repo first and then to the worker host's repo (`remote`), because launchd cannot read a
git dir on another volume and some branches exist only on the worker host. An object is present when any source has
it, missing only when every source that answered says so.
"""
from __future__ import annotations

import re
import shlex
from typing import Any, Callable, Dict, List, Optional

# nodes planned before this time may lack the section (their specs predate the rule); deploy time of R1
SINCE = "2026-11-07T16:00:00Z"
# review nodes take their input from the delivery under review; plan/decide nodes are not worker runs
CHECKED_KINDS = ("run", "check")
HEADING_RE = re.compile(r"^(#{1,6})\s*(.*)$")
ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
TOKEN_RE = re.compile(r"`(node|sha|file):([^`\s]+)`")
NONE_RE = re.compile(r"^(无|没有|none|n/?a)[。.；;，,]?\s*$", re.I)
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
ACCESS_ERR = ("not a git repository", "operation not permitted", "permission denied", "cannot change to",
              "no such file or directory", "could not resolve hostname", "connection", "timed out")


def section(spec_text: str) -> Optional[List[str]]:
    """The list items under the 前置条件 heading, or None when there is no such heading."""
    lines = (spec_text or "").splitlines()
    start = level = None
    for i, raw in enumerate(lines):
        m = HEADING_RE.match(raw.strip())
        if m and "前置条件" in m.group(2):
            start, level = i + 1, len(m.group(1))
            break
    if start is None:
        return None
    items: List[str] = []
    for raw in lines[start:]:
        m = HEADING_RE.match(raw.strip())
        if m and len(m.group(1)) <= level:
            break
        it = ITEM_RE.match(raw)
        if it:
            items.append(it.group(1))
        elif items and raw.startswith(("  ", "\t")) and raw.strip():
            items[-1] += " " + raw.strip()      # wrapped continuation of the previous item
    return items


def parse(spec_text: str) -> Dict[str, Any]:
    """-> {items: [{text, tokens: [(kind, value)]}], format: [problems]}"""
    items = section(spec_text)
    if items is None:
        return {"items": [], "format": ["说明里没有「前置条件」一节"]}
    if not items:
        return {"items": [], "format": ["「前置条件」一节是空的（不需要任何输入就写一项「无」）"]}
    if len(items) == 1 and NONE_RE.match(items[0].strip()):
        return {"items": [], "format": []}
    out, bad = [], []
    for i, text in enumerate(items, 1):
        toks = TOKEN_RE.findall(text)
        if not toks:
            bad.append("第 %d 项没有可核对的标记（`node:…` / `sha:…` / `file:…`）：%s" % (i, text[:60]))
        out.append({"text": text, "tokens": toks})
    return {"items": out, "format": bad}


def _git_answer(rc: int, err: str) -> Optional[bool]:
    """True present, False absent, None this source could not answer."""
    if rc == 0:
        return True
    low = (err or "").lower()
    if rc in (124, -9) or any(k in low for k in ACCESS_ERR):
        return None
    return False


def git_has(obj: str, repo: Optional[str], runner: Callable, remote: Optional[Dict[str, str]] = None) -> Optional[bool]:
    """Does any reachable copy of the repo have `obj` (a `<sha>^{commit}` or `<ref>:<path>`)?"""
    answers = []
    if repo:
        rc, _out, err = runner(["git", "-C", repo, "cat-file", "-e", obj], timeout=30)
        answers.append(_git_answer(rc, err))
        if answers[-1]:
            return True
    if remote and remote.get("host") and remote.get("repo"):
        cmd = "git -C %s cat-file -e %s" % (shlex.quote(remote["repo"]), shlex.quote(obj))
        argv = remote["ssh"](remote["host"], cmd) if remote.get("ssh") else \
            ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", remote["host"], cmd]
        rc, _out, err = runner(argv, timeout=30)
        answers.append(_git_answer(rc, err))
        if answers[-1]:
            return True
    if any(a is False for a in answers):
        return False
    return None


def node_met(state_lines: Dict[str, Any], line: str, ref: str) -> Optional[bool]:
    """`<id>` or `<LINE>/<id>`: delivered/reviewed PASS and not reviewed FAIL (via supersede). None: line unknown."""
    lid, nid = (ref.split("/", 1) if "/" in ref else (line, ref))
    ls = state_lines.get(lid)
    if ls is None:
        return None
    nodes = ls.get("nodes") or {}
    seen = set()
    while nid in nodes and nid not in seen:
        seen.add(nid)
        n = nodes[nid]
        if n.get("status") == "superseded" and n.get("by"):
            nid = n["by"]
            continue
        return n.get("status") in ("delivered", "reviewed") and n.get("outcome") == "PASS" \
            and n.get("review") != "FAIL"
    return False


def check(spec_text: str, spec_branch: str, line: str, state_lines: Dict[str, Any], repo: Optional[str],
          runner: Callable, remote: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    p = parse(spec_text)
    missing, unknown, items = [], [], []
    for it in p["items"]:
        res = []
        for kind, val in it["tokens"]:
            if kind == "node":
                ok = node_met(state_lines, line, val)
            elif kind == "sha":
                ok = git_has("%s^{commit}" % val, repo, runner, remote) if SHA_RE.match(val) else False
            else:
                ref, path = val.split("@", 1) if "@" in val else (spec_branch, val)
                ok = git_has("%s:%s" % (ref, path.lstrip("/")), repo, runner, remote) if ref and path else False
            label = "%s:%s" % (kind, val)
            res.append({"token": label, "ok": ok})
            if ok is False:
                missing.append(label)
            elif ok is None:
                unknown.append(label)
        items.append({"text": it["text"], "checks": res})
    return {"ok": not (missing or unknown or p["format"]), "items": items, "missing": missing,
            "unknown": unknown, "format": p["format"]}


def summary(res: Dict[str, Any], limit: int = 110) -> str:
    """One plain line for node_block / messages."""
    if res.get("format"):
        s = "说明的前置条件无法核对：" + "；".join(res["format"])
    elif res.get("missing"):
        s = "前置条件缺 %d 项：%s" % (len(res["missing"]), "，".join(res["missing"]))
    elif res.get("unknown"):
        s = "前置条件暂时查不了：%s" % "，".join(res["unknown"])
    else:
        s = "前置条件齐全"
    return s if len(s) <= limit else s[: limit - 1] + "…"
