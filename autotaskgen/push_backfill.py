#!/usr/bin/env python3
"""push_backfill: push the framework's work branches that origin lacks or trails, a few per run (WSL).

  python3 push_backfill.py [--repo DIR]                 dry run (default): JSON {candidates, ahead, blocked, next_batch, counts}
  python3 push_backfill.py --apply [--batch 10]         push the next N branches, one `git push` per branch

Branches: the ones checked out in the main tree and in every worktree (`git worktree list`), minus main/master and
detached heads, compared with one `git ls-remote --heads origin`:
  candidates  not on origin;   ahead  origin has an older commit of it (fast-forward);
  blocked     diverged from origin, origin's commit unknown here, or failing the push exclusions (workpush.Scanner:
              the same rules as dse_push.sh), each with its reason.
--apply never forces and never rebases; it stops at the first index-pack / network failure (GitHub rejects big
pushes) and records every result in ~/.progress/cache/push_backfill.json, so the next run picks up where it stopped.
A branch blocked at a sha stays skipped until its head moves.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import sys
from typing import Any, Dict, List, Optional

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import workpush  # noqa: E402

DEFAULT_REPO = os.path.expanduser("~/project/repo")
BATCH = 10


def state_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("PROGRESS_PUSH_BACKFILL_STATE") or
                        "~/.progress/cache/push_backfill.json").expanduser()


def load_state(p: pathlib.Path) -> Dict[str, Any]:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(p: pathlib.Path, d: Dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.new")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def survey(ctx: workpush.Ctx, repo: str) -> Dict[str, Any]:
    """Classify every checked-out branch against one ls-remote of origin."""
    wts = workpush._worktrees(ctx, repo)
    if not wts:
        raise RuntimeError("git worktree list failed in %s" % repo)
    branches: Dict[str, Dict[str, str]] = {}
    for wt in wts:
        ref = wt.get("branch") or ""
        if not ref.startswith("refs/heads/") or not wt.get("HEAD"):
            continue
        br = ref[len("refs/heads/"):]
        if br in workpush.PROTECTED or br in branches:
            continue
        branches[br] = {"branch": br, "sha": wt["HEAD"], "worktree": wt.get("worktree", "")}
    rc, out, err = ctx.git(repo, "ls-remote", "--heads", "origin")
    if rc != 0:
        raise RuntimeError("ls-remote origin failed: %s" % workpush.short(err))
    remote = {}
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            remote[parts[1][len("refs/heads/"):]] = parts[0]
    res = {"candidates": [], "ahead": [], "blocked": [], "up_to_date": 0, "behind": 0}
    for br, b in sorted(branches.items()):
        r = remote.get(br)
        if r is None:
            res["candidates"].append(dict(b, why="not on origin"))
            continue
        if r == b["sha"]:
            res["up_to_date"] += 1
            continue
        if ctx.git(repo, "cat-file", "-e", r + "^{commit}")[0] != 0:
            res["blocked"].append(dict(b, remote_sha=r, why="origin has %s, which this repo does not have (fetch first)"
                                       % r[:12]))
            continue
        if ctx.git(repo, "merge-base", "--is-ancestor", r, b["sha"])[0] == 0:
            _, n, _ = ctx.git(repo, "rev-list", "--count", "%s..%s" % (r, b["sha"]))
            res["ahead"].append(dict(b, remote_sha=r, ahead_by=int(n.strip() or 0)))
        elif ctx.git(repo, "merge-base", "--is-ancestor", b["sha"], r)[0] == 0:
            res["behind"] += 1
        else:
            res["blocked"].append(dict(b, remote_sha=r, why="diverged from origin (not a fast-forward); not forced"))
    res["total"] = len(branches)
    return res


def run(repo: str, apply: bool, batch: int, scan: bool = True, runner=None, timeout: float = 3600) -> Dict[str, Any]:
    ctx = workpush.Ctx(runner, timeout)
    sp = state_path()
    st = load_state(sp)
    done = st.setdefault("branches", {})
    sv = survey(ctx, repo)
    pushable = sorted(sv["candidates"] + sv["ahead"], key=lambda b: b["branch"])
    scanner = workpush.Scanner(repo, ctx, guard_host=None)
    next_batch: List[Dict[str, Any]] = []
    blocked = list(sv["blocked"])
    pushed, failed, stopped = [], [], None
    for b in pushable:
        prev = done.get(b["branch"]) or {}
        if prev.get("status") == "blocked" and prev.get("sha") == b["sha"]:
            blocked.append(dict(b, why=prev.get("why", "blocked earlier")))
            continue
        if apply and len(next_batch) >= batch:
            break  # --apply scans only as far as this run's batch
        if scan:
            verdict, why = scanner.scan(b["sha"])
            if verdict != "clean":
                blocked.append(dict(b, why=why))
                if verdict == "block" and apply:
                    done[b["branch"]] = {"sha": b["sha"], "status": "blocked", "why": why, "at": now()}
                continue
        if len(next_batch) < batch:
            next_batch.append(b)
    if apply:
        for b in next_batch:
            rc, _, err = ctx.git(repo, "push", "origin", "%s:refs/heads/%s" % (b["sha"], b["branch"]))
            rec = {"sha": b["sha"], "at": now()}
            if rc == 0:
                rec["status"] = "pushed"
                pushed.append(b["branch"])
            else:
                rec.update(status="failed", why=workpush.short(err))
                failed.append({"branch": b["branch"], "why": rec["why"]})
            done[b["branch"]] = rec
            st["updated_at"] = now()
            save_state(sp, st)
            if rc != 0 and workpush.is_network_failure(err):
                stopped = "stopped at %s: %s" % (b["branch"], rec["why"])
                break
        st["last_run"] = {"at": now(), "pushed": len(pushed), "failed": len(failed), "stopped": stopped}
        save_state(sp, st)
    strip = lambda xs: [{k: v for k, v in x.items() if k != "worktree"} for x in xs]  # noqa: E731
    out = {"ok": True, "repo": repo, "dry_run": not apply, "scanned": scan,
           "candidates": strip(sv["candidates"]), "ahead": strip(sv["ahead"]), "blocked": strip(blocked),
           "next_batch": [b["branch"] for b in next_batch],
           "counts": {"branches": sv["total"], "up_to_date": sv["up_to_date"], "behind": sv["behind"],
                      "missing": len(sv["candidates"]), "ahead": len(sv["ahead"]), "blocked": len(blocked),
                      "next_batch": len(next_batch)}}
    if apply:
        out.update(pushed=pushed, failed=failed, stopped=stopped, state=str(sp))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="push_backfill", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", default=os.environ.get("PROGRESS_PUSH_REPO", DEFAULT_REPO))
    p.add_argument("--apply", action="store_true", help="push (default: dry run, nothing pushed)")
    p.add_argument("--batch", type=int, default=BATCH, help="branches per run (default %d)" % BATCH)
    p.add_argument("--no-scan", action="store_true", help="dry run only: list without running the exclusions")
    p.add_argument("--timeout", type=float, default=3600, help="seconds for the whole run (default 3600)")
    a = p.parse_args(argv)
    if a.batch < 1:
        p.error("--batch must be >= 1")
    if a.apply and a.no_scan:
        p.error("--apply always runs the exclusions")
    try:
        res = run(a.repo, a.apply, a.batch, scan=not a.no_scan, timeout=a.timeout)
    except (RuntimeError, workpush.StepTimeout, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:300]}, ensure_ascii=False))
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if not res.get("failed") else 1


if __name__ == "__main__":
    sys.exit(main())
