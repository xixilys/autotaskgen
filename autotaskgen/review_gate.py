"""Delivery gate and automatic review nodes (router rules v2, R3, 11-07).

After a worker delivers a run/check node with PASS, patrol checks the delivery mechanically:

  1. the report named by --report exists in the delivered commit (--head);
  2. the report states the version it tested (`head=<sha>`), and that version is the delivered head, or an ancestor
     of it whose later commits only touch files in the report's own directory (the usual "commit the report last");
  3. every acceptance id the spec header names appears in the report, and the report carries command output
     (a fenced code block);
  4. every input listed under the spec's 前置条件 is mentioned in the report (its node id, sha or path).

A failed gate goes back to the router (the worker fixes and re-delivers, or the router plans rework). A passed gate
makes patrol plan 1-3 review nodes on Terra max. Each runs in a new session, reads only the spec, the report and the
diff, and writes `progress review --node <delivered> --review-node <itself>`. The node's review is the strictest
of its reviewers' verdicts once all of them have answered (progress_core.fold).
"""
from __future__ import annotations

import posixpath
import re
import shlex
from typing import Any, Callable, Dict, List, Optional, Tuple

import prereq

# deliveries before this time stay with the router (the rule starts at R3's deploy)
SINCE = "2026-11-07T16:10:00Z"
GATE_KINDS = ("run", "check")
GATE_PER_ROUND = 6            # deliveries gated per patrol round (each costs a few git calls over ssh)
MAX_REVIEWERS = 3
BIG_DIFF_LINES = 800
REVIEW_ROUTE = {"agent": "codex", "model": "codex/gpt-6.1-terra", "effort": "max"}
STRICT = {"PASS": 0, "UNKNOWN": 1, "PARTIAL": 2, "FAIL": 3}
HEAD_RE = re.compile(r"(?:tested[_ ]head|\bhead)\s*[=:：]\s*`?([0-9a-f]{7,40})\b", re.I)


def strictest(outcomes: List[str]) -> Optional[str]:
    known = [o for o in outcomes if o in STRICT]
    return max(known, key=lambda o: STRICT[o]) if known else None


class Git:
    """Ask the local repo first, then the worker host's repo (launchd cannot read a git dir on another volume, and
    some branches exist only on the worker host). run() -> (rc, out, err) of the first source that answered, or None
    when no source could answer."""

    def __init__(self, repo: Optional[str], runner: Callable, remote: Optional[Dict[str, Any]] = None):
        self.repo, self.runner, self.remote = repo, runner, remote

    def run(self, args: List[str]) -> Optional[Tuple[int, str, str]]:
        got = None
        if self.repo:
            rc, out, err = self.runner(["git", "-C", self.repo] + args, timeout=30)
            if prereq._git_answer(rc, err) is not None:
                if rc == 0:
                    return rc, out, err
                got = (rc, out, err)
        r = self.remote or {}
        if r.get("host") and r.get("repo"):
            cmd = "git -C %s %s" % (shlex.quote(r["repo"]), " ".join(shlex.quote(a) for a in args))
            argv = r["ssh"](r["host"], cmd) if r.get("ssh") else \
                ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", r["host"], cmd]
            rc, out, err = self.runner(argv, timeout=30)
            if prereq._git_answer(rc, err) is not None and (rc == 0 or got is None):
                return rc, out, err
        return got


def _same_sha(a: str, b: str) -> bool:
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def _mentions(text: str, needle: str) -> bool:
    return bool(needle) and re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(needle), text) is not None


def _needle(kind: str, val: str) -> str:
    if kind == "node":
        return val.split("/", 1)[-1]
    if kind == "sha":
        return val[:7]
    return val.split("@", 1)[-1].lstrip("/")


def gate(spec_text: str, header: Dict[str, Any], head: Optional[str], report: Optional[str], git: Git) -> Dict[str, Any]:
    """-> {ok, fails: [plain reasons], unknown: [what could not be checked], tested_head, report_text}"""
    fails: List[str] = []
    unknown: List[str] = []
    out: Dict[str, Any] = {"fails": fails, "unknown": unknown, "tested_head": None, "report_text": None}
    if not head or not report:
        fails.append("交付没带 %s" % "、".join(x for x, v in (("--head", head), ("--report", report)) if not v))
        out["ok"] = False
        return out
    r = git.run(["show", "%s:%s" % (head, report.lstrip("/"))])
    if r is None:
        unknown.append("报告")
        out["ok"] = False
        return out
    if r[0] != 0 or not r[1].strip():
        fails.append("报告 %s 不在交付的提交 %s 里" % (report, head[:9]))
        out["ok"] = False
        return out
    text = r[1]
    out["report_text"] = text
    rdir = posixpath.dirname(report.lstrip("/"))

    # 2. tested head
    cands = []
    for m in HEAD_RE.finditer(text):
        if m.group(1).lower() not in cands:
            cands.append(m.group(1).lower())
    if not cands:
        fails.append("报告没写测过的版本（写一行 `head=<测试时的 sha>`）")
    else:
        why = []
        for c in cands:
            if _same_sha(c, head):
                out["tested_head"] = c
                break
            anc = git.run(["merge-base", "--is-ancestor", c, head])
            if anc is None:
                unknown.append("测过的版本 %s" % c[:9])
                continue
            if anc[0] != 0:
                why.append("%s 不在交付版本的历史里" % c[:9])
                continue
            d = git.run(["diff", "--name-only", c, head])
            if d is None or d[0] != 0:
                unknown.append("测过的版本 %s 之后的改动" % c[:9])
                continue
            extra = [p for p in d[1].split() if not (rdir and p.startswith(rdir + "/"))]
            if not extra:
                out["tested_head"] = c
                break
            why.append("%s 之后还改了报告目录以外的 %s" % (c[:9], "，".join(extra[:3]) + ("等" if len(extra) > 3 else "")))
        if out["tested_head"]:
            unknown[:] = [u for u in unknown if not u.startswith("测过的版本")]
        elif why and not unknown:
            fails.append("测过的版本不是交付的版本：" + "；".join(why[:2]))

    # 3. acceptance ids + output tail
    ids = [str(a.get("id")) for a in header.get("acceptance") or [] if isinstance(a, dict) and a.get("id")]
    if ids:
        miss = [i for i in ids if not _mentions(text, i)]
        if miss:
            fails.append("报告没写这些验收项的结果：%s" % "，".join(miss))
        if "```" not in text:
            fails.append("报告里没有命令输出的尾部（用代码块贴出）")

    # 4. every prerequisite input has a conclusion in the report
    items = prereq.section(spec_text)
    if items:
        p = prereq.parse(spec_text)
        miss = ["%s:%s" % (k, v) for it in p["items"] for k, v in it["tokens"] if not _mentions(text, _needle(k, v))]
        if miss:
            fails.append("报告没交代这些前置条件：%s" % "，".join(miss))
    out["ok"] = not fails and not unknown
    return out


def diff_stat(git: Git, base: Optional[str], head: str) -> Optional[Dict[str, Any]]:
    """{paths, lines} changed from base to head, or None."""
    if not base:
        return None
    r = git.run(["diff", "--numstat", base, head])
    if r is None or r[0] != 0:
        return None
    paths, lines = [], 0
    for row in r[1].splitlines():
        parts = row.split("\t")
        if len(parts) == 3:
            paths.append(parts[2])
            lines += sum(int(x) for x in parts[:2] if x.isdigit())
    return {"paths": paths, "lines": lines}


def reviewer_count(header: Dict[str, Any], node: Dict[str, Any], paths: List[str], lines: Optional[int],
                   report: Optional[str]) -> Tuple[int, List[str]]:
    """1 for a small change; +1 for shared code (outside the spec's write_scope, else outside docs/), +1 for a node
    that serves a milestone, +1 for a big diff; at most MAX_REVIEWERS."""
    scope = [s.lstrip("/") for s in header.get("write_scope") or [] if isinstance(s, str) and s.strip()]
    rdir = posixpath.dirname((report or "").lstrip("/"))

    def own(p):
        if rdir and p.startswith(rdir + "/"):
            return True
        if scope:
            return any(p == s.rstrip("/") or p.startswith(s.rstrip("/") + "/") for s in scope)
        return p.startswith("docs/")
    why = []
    shared = [p for p in paths if not own(p)]
    if shared:
        why.append("改了共用代码 %s" % "，".join(shared[:2]))
    if node.get("milestones"):
        why.append("关键节点 %s" % "，".join(node["milestones"][:2]))
    if lines is not None and lines > BIG_DIFF_LINES:
        why.append("改动 %d 行" % lines)
    return min(MAX_REVIEWERS, 1 + len(why)), why


def review_id(nid: str, round_no: int, k: int) -> str:
    return "%s-review%d-%d" % (nid, round_no, k)


def review_spec(line: str, nid: str, rid: str, idx: int, total: int, head: str, report: str, base: Optional[str],
                spec_ref: Optional[str], marker: str) -> str:
    spec_cmd = ("`git show %s`" % spec_ref.replace("@", ":", 1)) if spec_ref and "@" in spec_ref else "（这单没有冻结说明）"
    diff_cmd = "`git diff %s %s`" % (base, head) if base else \
        "`git diff $(git merge-base origin/HEAD %s) %s`（找不到基线就看 `git show --stat %s` 往前几个提交）" % (head, head, head)
    return """%s
# 独立审查：%s/%s（第 %d/%d 个审查，本节点 %s）

你是这单交付的独立审查人，不是作者。只读下面三样，不重跑实验、不改文件、不提交、不推送：
1. 派单说明（作者接到的任务和验收项）：%s
2. 交付报告：`git show %s:%s`
3. 改动：%s；需要时读改动文件在交付版本的全文：`git show %s:<路径>`

不读作者的会话、终端或日志；报告里没写的，就当没做。逐项核对说明里的验收项：有没有对应的证据（命令、输出尾部、文件），结论和改动对不对得上，有没有越出说明的范围、改坏共用代码、把没做的说成做了。

结论只有四种：
- PASS：每个验收项都有证据，结论和改动一致；
- PARTIAL：做了，但有验收项缺证据或证据不足；
- FAIL：结论和改动或证据对不上、测试没真跑、改坏了别的东西；
- UNKNOWN：材料不够判断（在 summary 里写缺什么）。

写结论，这是你唯一要写的东西（命令 `progress`，没有就用 `~/.progress/bin/progress`）：
`progress review --line %s --node %s --review-node %s --outcome PASS|PARTIAL|FAIL|UNKNOWN --summary "一句白话：结论和最重要的理由"`
每个问题加一条 `--failure "<问题类别>"`（40 字以内，写类别不写描述）。写完就结束：不用 deliver，不用给 router 发消息。命令返回 "ok": false 就按 code 改参数重写一次。
""" % (marker, line, nid, idx, total, rid, spec_cmd, head, report, diff_cmd, head, line, nid, rid)
