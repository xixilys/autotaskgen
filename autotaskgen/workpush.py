#!/usr/bin/env python3
"""workpush: push a node's work branch to GitHub origin, after the fail-closed exclusions dse_push.sh applies.

`progress deliver` (worker) and `progress review` (router) call push_work_branch() before they write the event; the
event then carries `pushed` {branch, sha}, `push_failed` or `push_skipped`. push_backfill.py reuses Scanner.

Exclusions (the rules of the project's own push script (dse_push.sh) scan_tree, fail closed; differences from
dse_push: rule 3 scans only objects origin lacks, and registered false positives are let through by path + sha256):
  1. the named DiTPA reversal test and any path containing both "ditpa" and "holdout", in the pushed tree;
  2. the same names in any commit of the pushed history (git log --name-only);
  3. the publish guard (dot_sync_publish.sh --guard, "<path>\\n<blob>") on every blob origin does not have yet
     (`git rev-list --objects <head> --not --remotes=origin`: blobs of the new commits, including ones added and later
     deleted); only the secret categories salt-value / hard-content block. Blobs already on origin are not rescanned
     (11-06 lead L24: a tar.gz already on origin's branches blocked fe-dev). Known false positives are registered in
     an allow list (path + sha256 + why + approved_by): `.push-allow.json` in origin's main tree (in-repo, lands via
     the owners' process) and the scanning host's ~/.progress/push_allow.json (PROGRESS_PUSH_ALLOW); the pushed branch
     itself cannot allow anything. A block names the script, the rule, the path, blob and sha256.
  Clean verdicts are cached under the same keys dse_push uses (sha1 of "<blob>\\t<path>", "<commit>.ok"); dse_push's
  cache (~/.cache/dse_push_clean) is read, never written; ours is ~/.progress/cache/push_clean.
The guard script (and the salt values it checks against) exists only on WSL. Off WSL the blob texts the caches do
not cover go to `ssh wsl python3 <progress.py> guard-scan`, which runs the same guard there. Guard missing,
unreachable or erroring means nothing is pushed and the reason is returned.

Push: never --force, never main/master. A non-fast-forward rejection gets one `git pull --rebase origin <branch>`
(a conflict is aborted, nothing pushed), a fresh scan of the new head, and one more push. ~180 s in total.
Env: PROGRESS_PUSH=off disables it; PROGRESS_PUSH_GUARD, PROGRESS_PUSH_CACHE, PROGRESS_PUSH_DSE_CACHE override paths.
Nothing here prints file content: reasons name rules, categories and (for guard hits) the path only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

NAMED_RE = re.compile(r"test_ditpa_gem5_reversal|ditpa.*holdout|holdout.*ditpa", re.I)
BLOCK_CATS = ("salt-value", "hard-content")
# The guard's salt-value rule is a shape rule (a long id run and the salt keyword on one line), so a router that
# copies the raw category name next to the blob sha into STATE.md or a receipt trips it again (11-06: three files on
# fe-dev in a row). Block messages therefore name categories by a paste-safe label.
CAT_LABEL = {"salt-value": "S 类（留出集相关）", "hard-content": "H 类（敏感内容）"}
HITS_SHOWN = 6
DEFAULT_GUARD = os.path.expanduser("~/opt/publish-guard/guard.sh")  # set PROGRESS_PUSH_GUARD
REMOTE_CLI = os.environ.get("PROGRESS_REMOTE_CLI", "~/opt/progress/progress.py")
PROTECTED = ("main", "master")
TIMEOUT_S = 180
ALLOW_REPO_FILE = ".push-allow.json"
ALLOW_REPO_REFS = ("refs/remotes/origin/main", "refs/remotes/origin/master")
REMOTE_CHUNK_BYTES = 8 << 20      # per guard-scan ssh call
REMOTE_MAX_BYTES = 64 << 20       # more unscanned content than this from off-WSL: refuse (push from WSL / backfill)
ZERO = "0" * 40
NET_FAIL_RE = re.compile(r"index-pack|unpack|RPC failed|early EOF|hung up|Could not resolve|Connection|timed out|"
                         r"unable to access|SSL|HTTP 5\d\d|Operation too slow", re.I)

Runner = Callable[[List[str], Optional[str], Optional[bytes], float, Dict[str, str]], Tuple[int, bytes, bytes]]


class StepTimeout(Exception):
    pass


def default_runner(cmd, cwd, data, timeout, env):
    try:
        r = subprocess.run(cmd, cwd=cwd, input=data if data is not None else None,
                           stdin=subprocess.DEVNULL if data is None else None,
                           capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise StepTimeout(" ".join(cmd[:3]))
    except OSError as exc:
        return 127, b"", str(exc).encode()
    return r.returncode, r.stdout, r.stderr


def enabled() -> bool:
    return os.environ.get("PROGRESS_PUSH", "on").strip().lower() not in ("off", "0", "no", "false")


def guard_path() -> str:
    return os.environ.get("PROGRESS_PUSH_GUARD") or DEFAULT_GUARD


def own_cache() -> pathlib.Path:
    return pathlib.Path(os.environ.get("PROGRESS_PUSH_CACHE") or "~/.progress/cache/push_clean").expanduser()


def dse_cache() -> pathlib.Path:
    return pathlib.Path(os.environ.get("PROGRESS_PUSH_DSE_CACHE") or "~/.cache/dse_push_clean").expanduser()


def allow_store_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("PROGRESS_PUSH_ALLOW") or
                        os.path.join(os.environ.get("PROGRESS_HOME") or os.path.expanduser("~/.progress"), "push_allow.json"))


def _allow_rows(doc: Any, source: str) -> List[Dict[str, str]]:
    rows = doc.get("allow") if isinstance(doc, dict) else doc
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        vals = {k: str(r.get(k) or "").strip() for k in ("path", "sha256", "why", "approved_by")}
        if all(vals.values()) and re.fullmatch(r"[0-9a-f]{64}", vals["sha256"].lower()):
            vals["sha256"] = vals["sha256"].lower()
            vals["source"] = source
            out.append(vals)
    return out


def blob_key(blob: str, path: str) -> str:
    return hashlib.sha1(("%s\t%s" % (blob, path)).encode("utf-8", "surrogateescape")).hexdigest()


def short(text: Any, n: int = 300) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    lines = [l.strip() for l in str(text).splitlines() if l.strip() and not l.strip().startswith("hint:")]
    return " | ".join(lines)[:n]


class Deadline:
    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds

    def left(self) -> float:
        return self.end - time.monotonic()


class Ctx:
    """Command runner with one shared deadline; git runs with no prompts, no editor and C messages."""

    def __init__(self, runner: Optional[Runner] = None, timeout: float = TIMEOUT_S):
        self.runner = runner or default_runner
        self.deadline = Deadline(timeout)

    def run(self, cmd: List[str], cwd: Optional[str] = None, data: Optional[bytes] = None,
            git: bool = False) -> Tuple[int, bytes, bytes]:
        left = self.deadline.left()
        if left < 1:
            raise StepTimeout(" ".join(cmd[:3]))
        env = dict(os.environ)
        if git:
            env.update(GIT_TERMINAL_PROMPT="0", GIT_EDITOR="true", GIT_SEQUENCE_EDITOR="true", LC_ALL="C")
        return self.runner(cmd, cwd, data, left, env)

    def git(self, cwd: str, *args: str, data: Optional[bytes] = None) -> Tuple[int, str, str]:
        rc, out, err = self.run(["git", "-C", cwd] + list(args), data=data, git=True)
        return rc, out.decode("utf-8", "surrogateescape"), err.decode("utf-8", "replace")


# ---------------------------------------------------------------- scan


class Scanner:
    """scan(commit) -> ("clean"|"block"|"error", reason). guard_host: ssh host for the guard when it is not local."""

    def __init__(self, repo: str, ctx: Ctx, guard: Optional[str] = None, guard_host: Optional[str] = None,
                 remote_cli: str = REMOTE_CLI):
        self.repo, self.ctx = repo, ctx
        self.guard = guard or guard_path()
        self.guard_host = guard_host
        self.remote_cli = remote_cli
        self.own, self.dse = own_cache(), dse_cache()
        self.note = ""
        self.allowed: List[Dict[str, str]] = []   # allow-list hits of the last scan (path, sha256, source)
        self._ok_list: Optional[List[str]] = None
        self._allow: Optional[List[Dict[str, str]]] = None

    # allow list (never from the branch being pushed)
    def allow_list(self) -> List[Dict[str, str]]:
        if self._allow is not None:
            return self._allow
        rows: List[Dict[str, str]] = []
        for ref in ALLOW_REPO_REFS:
            rc, out, _ = self.ctx.git(self.repo, "show", "%s:%s" % (ref, ALLOW_REPO_FILE))
            if rc == 0:
                try:
                    rows += _allow_rows(json.loads(out), "%s:%s" % (ref.split("/", 2)[2], ALLOW_REPO_FILE))
                except ValueError:
                    pass
                break
        f = allow_store_path()
        try:
            rows += _allow_rows(json.loads(f.read_text(encoding="utf-8")), str(f))
        except (OSError, ValueError):
            pass
        self._allow = rows
        return rows

    def _allowed(self, blob: str, path: str) -> Optional[Dict[str, str]]:
        rows = [r for r in self.allow_list() if r["path"] == path]
        if not rows:
            return None
        rc, out, _ = self.ctx.run(["git", "-C", self.repo, "cat-file", "blob", blob], git=True)
        if rc != 0:
            return None
        h = hashlib.sha256(out).hexdigest()
        return next((dict(r, blob=blob) for r in rows if r["sha256"] == h), None)

    def _hit_row(self, cats: Any, blob: str, path: str) -> Tuple[str, str, str, str]:
        rc, out, _ = self.ctx.run(["git", "-C", self.repo, "cat-file", "blob", blob], git=True)
        h = hashlib.sha256(out).hexdigest() if rc == 0 else "?"
        label = "、".join(CAT_LABEL.get(c, c) for c in str(cats or "").split()) or "?"
        return label, path, blob, h

    def _hits(self, rows: List[Tuple[str, str, str, str]]) -> str:
        """One message for every blocked file of this push (not just the first), paste-safe."""
        shown = ["%s %s（blob %s，sha256 %s）" % (lab, path, blob[:12], h) for lab, path, blob, h in rows[:HITS_SHOWN]]
        if len(rows) > HITS_SHOWN:
            shown.append("另有 %d 个" % (len(rows) - HITS_SHOWN))
        return ("publish guard 拦下 %d 个文件：%s。检查脚本 %s --guard，由 autotaskgen workpush.py 第 3 条规则"
                "（只扫 origin 没有的对象）调用。误报：把 path + sha256 + why + approved_by 登记到 %s（origin main）"
                "或 %s。被拦的版本已经提交在要推的历史里时，之后再改文字解不了（旧版本仍会被推上去），只能登记放行或改写历史。"
                "记录这次拦截时照抄本条的类别代号，别写检查的原始类别名。"
                % (len(rows), "；".join(shown), self.guard, ALLOW_REPO_FILE, allow_store_path()))

    # caches
    def _cached(self, key: str) -> bool:
        return (self.own / "blobs" / key).exists() or (self.dse / "blobs" / key).exists()

    def _mark(self, key: str) -> None:
        try:
            (self.own / "blobs").mkdir(parents=True, exist_ok=True)
            (self.own / "blobs" / key).touch()
        except OSError:
            pass

    def commit_ok(self, c: str) -> bool:
        return (self.own / ("%s.ok" % c)).exists() or (self.dse / ("%s.ok" % c)).exists()

    def _mark_commit(self, c: str) -> None:
        try:
            self.own.mkdir(parents=True, exist_ok=True)
            (self.own / ("%s.ok" % c)).touch()
        except OSError:
            pass

    def _ok_commits(self) -> List[str]:
        """Cached clean commits present in this repo (their whole history already passed the name check)."""
        if self._ok_list is not None:
            return self._ok_list
        names = set()
        for d in (self.own, self.dse):
            try:
                names.update(p.name[:-3] for p in d.iterdir() if p.name.endswith(".ok") and len(p.name) == 43)
            except OSError:
                pass
        ok: List[str] = []
        if names:
            rc, out, _ = self.ctx.git(self.repo, "cat-file", "--batch-check",
                                      data=("".join(n + "\n" for n in sorted(names))).encode())
            if rc == 0:
                ok = [ln.split()[0] for ln in out.splitlines() if ln.split()[1:2] == ["commit"]]
        self._ok_list = ok
        return ok

    # rules 1-3
    def scan(self, c: str) -> Tuple[str, str]:
        try:
            return self._scan(c)
        except StepTimeout as exc:
            return "error", "scan timed out (%s)" % exc

    def _scan(self, c: str) -> Tuple[str, str]:
        pairs: Dict[str, Tuple[str, str]] = {}
        full = not self.commit_ok(c)
        if full:
            rc, out, err = self.ctx.git(self.repo, "ls-tree", "-r", c)
            if rc != 0:
                return "error", "ls-tree %s failed: %s" % (c[:12], short(err))
            named = 0
            for ln in out.splitlines():
                meta, _, path = ln.partition("\t")
                if NAMED_RE.search(path):
                    named += 1
            if named:
                return "block", ("named exclusion in the pushed tree (%d path(s): the DiTPA reversal test or a "
                                 "ditpa+holdout path); remove them from the branch" % named)
            stdin = c + "\n" + "".join("^%s\n" % o for o in self._ok_commits() if o != c)
            rc, out, err = self.ctx.git(self.repo, "log", "--format=", "--name-only", "--stdin", data=stdin.encode())
            if rc != 0:
                return "error", "git log of %s failed: %s" % (c[:12], short(err))
            if any(NAMED_RE.search(l) for l in out.splitlines()):
                return "block", ("named exclusion in the pushed history (a commit touches the DiTPA reversal test or a "
                                 "ditpa+holdout path); that history cannot go to origin")
        # content scan: only objects origin does not have (the new commits' blobs, also ones added and later deleted)
        rc, out, err = self.ctx.git(self.repo, "rev-list", "--objects", "--no-object-names", c, "--not", "--remotes=origin")
        if rc != 0:
            return "error", "git rev-list --objects of %s failed: %s" % (c[:12], short(err))
        new = set(out.split())
        rc, out, err = self.ctx.git(self.repo, "rev-list", "--objects", c, "--not", "--remotes=origin")
        if rc != 0:
            return "error", "git rev-list --objects of %s failed: %s" % (c[:12], short(err))
        named = {}
        for ln in out.splitlines():
            oid, _, path = ln.partition(" ")
            if path and oid in new:
                named.setdefault(oid, path)
        if named:
            rc, out, err = self.ctx.git(self.repo, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                                        data="".join(o + "\n" for o in named).encode())
            if rc != 0:
                return "error", "cat-file --batch-check failed: %s" % short(err)
            blobs = {ln.split()[0] for ln in out.splitlines() if ln.split()[1:2] == ["blob"]}
            pairs = {blob_key(o, p): (o, p) for o, p in named.items() if o in blobs}
        else:
            pairs = {}
        self.allowed = []
        todo = []
        for k, (b, p) in pairs.items():
            if self._cached(k):
                continue
            a = self._allowed(b, p)
            if a:
                self.allowed.append({"path": p, "sha256": a["sha256"], "source": a["source"]})
                continue
            todo.append((k, b, p))
        verdict, reason = self._guard_all(todo)
        if verdict == "clean" and full:
            self._mark_commit(c)
        return verdict, reason

    # guard
    def _guard_all(self, todo: List[Tuple[str, str, str]]) -> Tuple[str, str]:
        if not todo:
            return "clean", ""
        if os.path.isfile(self.guard) and os.access(self.guard, os.X_OK):
            hits = []
            for key, blob, path in todo:
                text = self._text(blob, path)
                if text is None:
                    return "error", "cat-file %s failed" % blob[:12]
                v, cats = guard_text(self.ctx, self.guard, text)
                if v == "clean":
                    self._mark(key)
                elif v == "block":
                    hits.append(self._hit_row(cats, blob, path))
                else:
                    return "error", "publish guard error (%s) on %s" % (cats, path)
            return ("block", self._hits(hits)) if hits else ("clean", "")
        if not self.guard_host:
            return "error", "publish guard not found (%s); nothing pushed" % self.guard
        return self._guard_remote(todo)

    def _text(self, blob: str, path: str) -> Optional[bytes]:
        rc, out, _ = self.ctx.run(["git", "-C", self.repo, "cat-file", "blob", blob], git=True)
        return path.encode("utf-8", "surrogateescape") + b"\n" + out if rc == 0 else None

    def _remote(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        cmd = "python3 %s guard-scan" % shlex.quote(self.remote_cli)
        rc, out, err = self.ctx.run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", self.guard_host, cmd],
                                    data=json.dumps(payload).encode())
        lines = out.decode("utf-8", "replace").strip().splitlines()
        try:
            res = json.loads(lines[-1]) if lines else None
        except ValueError:
            res = None
        if not isinstance(res, dict):
            raise RuntimeError("guard host %s: no answer (rc=%d) %s" % (self.guard_host, rc, short(err, 200)))
        if not res.get("ok"):
            raise RuntimeError("guard host %s: %s" % (self.guard_host, short(res.get("error"), 200)))
        return res

    def _guard_remote(self, todo: List[Tuple[str, str, str]]) -> Tuple[str, str]:
        try:
            have = set(self._remote({"op": "cached", "keys": [k for k, _, _ in todo]}).get("cached") or [])
            for k in have:
                self._mark(k)
            todo = [t for t in todo if t[0] not in have]
            total, chunk, size, hits = 0, [], 0, []
            for i, (key, blob, path) in enumerate(todo):
                text = self._text(blob, path)
                if text is None:
                    return "error", "cat-file %s failed" % blob[:12]
                total += len(text)
                if total > REMOTE_MAX_BYTES:
                    return "error", ("more than %d MB of unscanned content; push this branch from WSL or let "
                                     "push_backfill do it" % (REMOTE_MAX_BYTES >> 20))
                chunk.append({"key": key, "path": path, "b64": base64.b64encode(text).decode()})
                size += len(text)
                if size >= REMOTE_CHUNK_BYTES or i == len(todo) - 1:
                    res = self._remote({"op": "scan", "items": chunk})
                    verdicts = res.get("verdicts") or {}
                    for it in chunk:
                        v = verdicts.get(it["key"]) or {}
                        if v.get("verdict") == "clean":
                            self._mark(it["key"])
                        elif v.get("verdict") == "block":
                            blob = next(b for k2, b, _ in todo if k2 == it["key"])
                            hits.append(self._hit_row(v.get("cats"), blob, it["path"]))
                        else:
                            return "error", "publish guard error (%s) on %s" % (v.get("cats") or "no verdict",
                                                                               it["path"])
                    chunk, size = [], 0
            return ("block", self._hits(hits)) if hits else ("clean", "")
        except StepTimeout as exc:
            return "error", "guard host timed out (%s)" % exc
        except RuntimeError as exc:
            return "error", str(exc)


def guard_text(ctx: Ctx, guard: str, text: bytes) -> Tuple[str, str]:
    """dot_sync_publish.sh --guard -: 0 clean; 1 hit (only salt-value / hard-content block); else error."""
    rc, out, _ = ctx.run([guard, "--guard", "-"], data=text)
    if rc == 0:
        return "clean", ""
    if rc == 1:
        cats = " ".join(l[4:].strip() for l in out.decode("utf-8", "replace").splitlines() if l.startswith("HIT "))
        if any(c in (" %s " % cats) for c in BLOCK_CATS):
            return "block", " ".join(c for c in BLOCK_CATS if c in cats)
        return "clean", ""
    return "error", "rc=%d" % rc


def serve_guard_scan(payload: Dict[str, Any], runner: Optional[Runner] = None) -> Dict[str, Any]:
    """The guard host's side of `progress guard-scan` (stdin JSON). op=cached: which keys are already clean;
    op=scan: run the local guard on each {key, path, b64} text, stop at the first block/error."""
    own, dse = own_cache(), dse_cache()
    op = payload.get("op")
    if op == "cached":
        keys = [k for k in payload.get("keys") or [] if isinstance(k, str) and re.fullmatch(r"[0-9a-f]{40}", k)]
        return {"ok": True, "cached": [k for k in keys if (own / "blobs" / k).exists() or (dse / "blobs" / k).exists()]}
    if op != "scan":
        return {"ok": False, "error": "unknown op %r" % op}
    guard = guard_path()
    if not (os.path.isfile(guard) and os.access(guard, os.X_OK)):
        return {"ok": False, "error": "publish guard not found on this host (%s)" % guard}
    ctx = Ctx(runner, timeout=TIMEOUT_S)
    verdicts: Dict[str, Dict[str, str]] = {}
    for it in payload.get("items") or []:
        key = str(it.get("key") or "")
        try:
            text = base64.b64decode(it.get("b64") or "")
        except ValueError:
            verdicts[key] = {"verdict": "error", "cats": "bad payload"}
            break
        path = str(it.get("path") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", key) or not text.startswith(path.encode("utf-8", "surrogateescape") + b"\n"):
            verdicts[key] = {"verdict": "error", "cats": "bad item"}
            break
        try:
            v, cats = guard_text(ctx, guard, text)
        except StepTimeout:
            v, cats = "error", "timeout"
        verdicts[key] = {"verdict": v, "cats": cats}
        if v != "clean":
            break
        try:
            (own / "blobs").mkdir(parents=True, exist_ok=True)
            (own / "blobs" / key).touch()
        except OSError:
            pass
    return {"ok": True, "verdicts": verdicts}


# ---------------------------------------------------------------- push


def _worktrees(ctx: Ctx, top: str) -> List[Dict[str, str]]:
    rc, out, _ = ctx.git(top, "worktree", "list", "--porcelain")
    if rc != 0:
        return []
    res, cur = [], {}
    for ln in out.splitlines() + [""]:
        if not ln.strip():
            if cur:
                res.append(cur)
            cur = {}
            continue
        k, _, v = ln.partition(" ")
        cur[k] = v or "1"
    return res


def resolve(ctx: Ctx, cwd: str, head: Optional[str] = None) -> Dict[str, Any]:
    """Repo top, branch and sha of cwd, or of the worktree whose HEAD is `head` when cwd's is not."""
    rc, out, _ = ctx.git(cwd, "rev-parse", "--show-toplevel")
    if rc != 0:
        return {"why": "not a git work tree: %s" % cwd}
    top = out.strip()
    rc, sha, _ = ctx.git(top, "rev-parse", "--verify", "-q", "HEAD")
    sha = sha.strip()
    if head and not sha.startswith(head) and not head.startswith(sha or "-"):
        rc, full, _ = ctx.git(top, "rev-parse", "--verify", "-q", head + "^{commit}")
        full = full.strip()
        if rc == 0 and full:
            for wt in _worktrees(ctx, top):
                if wt.get("HEAD") == full and wt.get("branch") and os.path.isdir(wt.get("worktree", "")):
                    top, sha = wt["worktree"], full
                    break
    rc, br, _ = ctx.git(top, "symbolic-ref", "-q", "--short", "HEAD")
    if rc != 0 or not br.strip():
        return {"top": top, "sha": sha, "why": "detached HEAD (no branch to push)"}
    return {"top": top, "sha": sha, "branch": br.strip()}


def _remote_sha(ctx: Ctx, top: str, branch: str) -> Tuple[Optional[str], Optional[str]]:
    rc, out, err = ctx.git(top, "ls-remote", "origin", "refs/heads/" + branch)
    if rc != 0:
        return None, "ls-remote origin failed: %s" % short(err)
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[1] == "refs/heads/" + branch:
            return parts[0], None
    return "", None


def _rebase_dir(ctx: Ctx, top: str) -> bool:
    for name in ("rebase-merge", "rebase-apply"):
        rc, out, _ = ctx.git(top, "rev-parse", "--git-path", name)
        p = out.strip()
        if rc == 0 and p and os.path.exists(p if os.path.isabs(p) else os.path.join(top, p)):
            return True
    return False


def push_work_branch(cwd: str, head: Optional[str] = None, runner: Optional[Runner] = None,
                     timeout: float = TIMEOUT_S, guard: Optional[str] = None,
                     guard_host: Optional[str] = None) -> Dict[str, Any]:
    """Push the branch of cwd (or of the worktree holding `head`) to origin after the exclusions.
    Returns {"status": "pushed"|"failed"|"skipped", "branch", "sha", "remote": "origin", "error"|"why", ...}.
    Never raises; nothing is pushed unless every check passed."""
    res: Dict[str, Any] = {"status": "skipped", "remote": "origin"}
    if not enabled():
        return dict(res, why="PROGRESS_PUSH=off")
    ctx = Ctx(runner, timeout)
    try:
        return _push(ctx, cwd, head, guard, guard_host, res)
    except StepTimeout as exc:
        return dict(res, status="failed", error="push step timed out after %ds (%s); not pushed" % (timeout, exc))
    except Exception as exc:  # fail closed
        return dict(res, status="failed", error="push internal error: %s: %s" % (type(exc).__name__, short(exc, 200)))


def _push(ctx: Ctx, cwd: str, head: Optional[str], guard: Optional[str], guard_host: Optional[str],
          res: Dict[str, Any]) -> Dict[str, Any]:
    r = resolve(ctx, cwd, head)
    res.update({k: r[k] for k in ("branch", "sha") if r.get(k)})
    if r.get("why"):
        return dict(res, why=r["why"])
    top, branch, sha = r["top"], r["branch"], r["sha"]
    res["worktree"] = top
    if branch in PROTECTED:
        return dict(res, why="branch is %s (never pushed from here)" % branch)
    rc, url, _ = ctx.git(top, "remote", "get-url", "origin")
    if rc != 0 or not url.strip():
        return dict(res, why="no origin remote")
    rc, dirty, _ = ctx.git(top, "status", "--porcelain", "--untracked-files=no")
    if rc == 0 and dirty.strip():
        res["uncommitted"] = True  # only committed work is pushed
    remote, err = _remote_sha(ctx, top, branch)
    if err:
        return dict(res, status="failed", error=err)
    if remote == sha:
        return dict(res, status="pushed", already=True)
    scanner = Scanner(top, ctx, guard=guard, guard_host=guard_host)
    old = sha
    for attempt in (1, 2):
        verdict, reason = scanner.scan(sha)
        if scanner.note:
            res["note"] = scanner.note
        if scanner.allowed:
            res["allowed"] = scanner.allowed  # allow-list entries that let a guard hit through (path, sha256, source)
        if verdict != "clean":
            what = "blocked by the push exclusions" if verdict == "block" else "push check failed"
            return dict(res, status="failed", error="%s: %s; not pushed" % (what, reason))
        rc, _, perr = ctx.git(top, "push", "origin", "%s:refs/heads/%s" % (sha, branch))
        if rc == 0:
            break
        nonff = "[rejected]" in perr and ("non-fast-forward" in perr or "fetch first" in perr)
        if not nonff or attempt == 2:
            return dict(res, status="failed", error="git push failed: %s" % short(perr))
        # one rebase onto origin's branch, then scan and push again
        rc, _, rerr = ctx.git(top, "-c", "rebase.autoStash=false", "pull", "--rebase", "origin", branch)
        if rc != 0:
            aborted = False
            if _rebase_dir(ctx, top):
                ctx.git(top, "rebase", "--abort")
                aborted = True
            why = "rebase onto origin/%s conflicted; rebase aborted, branch unchanged" % branch if aborted else \
                "git pull --rebase failed: %s" % short(rerr, 200)
            return dict(res, status="failed", error="non-fast-forward: %s; not pushed" % why)
        rc, new, _ = ctx.git(top, "rev-parse", "HEAD")
        sha = new.strip()
        res.update(sha=sha, rebased=True, old_sha=old)
    remote, err = _remote_sha(ctx, top, branch)
    if err:
        return dict(res, status="failed", error="pushed, but could not verify: %s" % err)
    if remote != sha:
        return dict(res, status="failed", error="origin shows %s at %s after the push, not %s" %
                    (branch, (remote or "nothing")[:12], sha[:12]))
    return dict(res, status="pushed")


def event_fields(res: Dict[str, Any]) -> Dict[str, Any]:
    """The push result as event fields: pushed {branch, sha} | push_failed | push_skipped."""
    if res.get("status") == "pushed":
        return {"pushed": {"branch": res["branch"], "sha": res["sha"]}}
    if res.get("status") == "failed":
        return {"push_failed": short(res.get("error") or "unknown", 400) or "unknown"}
    return {"push_skipped": short(res.get("why") or "skipped", 200) or "skipped"}


def is_network_failure(text: str) -> bool:
    return bool(NET_FAIL_RE.search(text or ""))

