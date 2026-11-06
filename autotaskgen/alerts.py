"""QQ alerts for things that need a human (or that mean the patrol itself is blind), with dedupe.

State file $PROGRESS_HOME/alerts.json (default ~/.progress/alerts.json):
  {key: {first_at, last_sent_at, sends, text, resolved_at}}

sync(active, now, runner, qq) is called once per patrol round with {key: text} of everything that is wrong now:
- key active and new (or resolved before and active again): send now "[需要你看] <text>".
- key active and already sent: resend 2 h, 8 h, 24 h after first_at (REMIND_H; at most 4 sends in all, then
  silent but kept while it stays active).
- key no longer active but was sent: send once "[已恢复] <first line of text>" and mark it resolved ("[不用再管] …"
  for a supervisor request to the human, key "fu:…").
- Requests to the human ("fu:…") are reworded by the supervisor and come back under a new key. A new fu key whose
  text is close to another unresolved fu record of the same line (difflib ratio >= FU_SAME) takes over that record
  (first_at, sends: same reminder pacing) and nothing new is sent; the old record is closed silently. A fu key that
  drops out waits FU_GONE_S before its "[不用再管]" goes out, so a reworded comeback merges instead.
  (11-06: the Instabox ask went "[不用再管]" at 22:34 and "[需要你看]" reworded at 22:52; FE/SR/NEWP asks were sent
  twice under two wordings.)
- At most ONE QQ message per round: every due text is merged into one list (<= MAX_CHARS); texts that do not
  fit stay due and go out next round. A failed send changes no state, so it is retried next round.
- qq.json missing -> nothing is sent or recorded; with something active the record is {"code": "qq_unconfigured"}.
Uses decide.send_qq (same qq.json, same HERMES_BIN). Never raises.
"""
from __future__ import annotations

import datetime as _dt
import difflib
import json
import os
import pathlib
import sys
from typing import Any, Dict, List, Optional

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import decide  # noqa: E402
import progress_core as core  # noqa: E402

REMIND_H = (2, 8, 24)          # resend this long after first_at; len + 1 = max sends
MAX_CHARS = 1500
ITEM_MAX = 300
KEEP_RESOLVED_S = 7 * 86400
FU_SAME = 0.5                  # 11-07 history: rewordings of one ask 0.55-0.98, different asks of a line <= 0.43
FU_GONE_S = 60 * 60


def path() -> pathlib.Path:
    return core.home() / "alerts.json"


def load() -> Dict[str, Dict[str, Any]]:
    try:
        doc = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in doc.items() if isinstance(v, dict)} if isinstance(doc, dict) else {}


def save(state: Dict[str, Dict[str, Any]]) -> None:
    p = path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=1), encoding="utf-8")
        os.replace(str(tmp), str(p))
    except OSError:
        pass


def _iso(t: _dt.datetime) -> str:
    return t.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


def _clip(text: str, n: int = ITEM_MAX) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _due(rec: Dict[str, Any], now: _dt.datetime) -> bool:
    """Active, already sent: is the next reminder due?"""
    sends = int(rec.get("sends") or 0)
    if sends < 1 or sends > len(REMIND_H):
        return False
    first = core.parse_iso(rec.get("first_at"))
    return first is not None and (now - first).total_seconds() >= REMIND_H[sends - 1] * 3600


def _fu_line(text: str) -> str:
    return str(text or "").split("线：", 1)[0] if "线：" in str(text or "") else ""


def _fu_merge(active: Dict[str, str], state: Dict[str, Dict[str, Any]], now: _dt.datetime) -> None:
    """New fu keys that reword an unresolved fu record of the same line take that record over (in place)."""
    for key in sorted(active):
        rec = state.get(key)
        if not key.startswith("fu:") or (rec is not None and not rec.get("resolved_at")):
            continue
        text = str(active[key])
        best, score = None, FU_SAME
        for old, orec in state.items():
            if old == key or not old.startswith("fu:") or orec.get("resolved_at") or not int(orec.get("sends") or 0):
                continue
            otext = str(orec.get("text") or "")
            if not _fu_line(text) or _fu_line(text) != _fu_line(otext):
                continue
            r = difflib.SequenceMatcher(None, text, otext).ratio()
            if r >= score:
                best, score = old, r
        if best is None:
            continue
        orec = state[best]
        state[key] = {"first_at": orec.get("first_at"), "last_sent_at": orec.get("last_sent_at"),
                      "sends": orec.get("sends"), "text": text, "resolved_at": None, "same_as": best}
        orec["resolved_at"], orec["merged_into"] = _iso(now), key


def sync(active: Dict[str, str], now: _dt.datetime, runner, qq: Optional[Dict[str, str]]) -> List[Dict[str, Any]]:
    """-> records [{key, kind: new|remind|resolved, text, sent, error?}] or [{code: qq_unconfigured}]."""
    try:
        return _sync(active or {}, now, runner, qq)
    except Exception as exc:  # noqa: BLE001 - alerts must never sink a patrol round
        return [{"code": "alerts_failed", "error": str(exc)[:200]}]


def _sync(active: Dict[str, str], now: _dt.datetime, runner, qq: Optional[Dict[str, str]]) -> List[Dict[str, Any]]:
    if not qq:
        if not active:
            return []
        return [{"code": "qq_unconfigured", "why": "~/.progress/qq.json 缺失：告警不会发到 QQ"}]
    state = load()
    _fu_merge(active, state, now)
    items: List[Dict[str, Any]] = []   # due messages, in send order: new, remind, resolved
    for key in sorted(active):
        text = str(active[key])
        rec = state.get(key)
        if rec is not None:
            rec.pop("gone_at", None)
        if rec is None or rec.get("resolved_at"):
            items.append({"key": key, "kind": "new", "text": text, "line": "[需要你看] " + _clip(text)})
        elif _due(rec, now):
            hrs = int((now - (core.parse_iso(rec.get("first_at")) or now)).total_seconds() // 3600)
            items.append({"key": key, "kind": "remind", "text": text,
                          "line": "[需要你看] %s（已持续 %d 小时）" % (_clip(text, ITEM_MAX - 20), hrs)})
        elif rec is not None:
            rec["text"] = text   # keep the latest wording for the 已恢复 message
    for key in sorted(state):
        rec = state[key]
        if key not in active and int(rec.get("sends") or 0) >= 1 and not rec.get("resolved_at"):
            if key.startswith("fu:"):
                gone = core.parse_iso(rec.get("gone_at"))
                if gone is None:
                    rec["gone_at"] = _iso(now)
                    continue
                if (now - gone).total_seconds() < FU_GONE_S:
                    continue
            first = _first_line(rec.get("text") or key)
            # a request to the human that dropped out: say it is no longer needed, not that "it recovered" (which
            # read as if the human's answer had arrived)
            line = ("[不用再管] " + _clip(first, ITEM_MAX - 30) + "（这件事已办完，或该线现在不等你了）") if key.startswith("fu:") \
                else "[已恢复] " + _clip(first)
            items.append({"key": key, "kind": "resolved", "text": rec.get("text") or key, "line": line})
    order = {"new": 0, "remind": 1, "resolved": 2}
    items.sort(key=lambda i: order[i["kind"]])

    out: List[Dict[str, Any]] = []
    chosen: List[Dict[str, Any]] = []
    total = 0
    for it in items:
        add = len(it["line"]) + 6
        if chosen and total + add > MAX_CHARS - 40:
            continue   # does not fit: stays due, goes out next round
        chosen.append(it)
        total += add
    if chosen:
        if len(chosen) == 1:
            msg = chosen[0]["line"]
        else:
            msg = "巡检提醒（%d 条）：\n" % len(chosen) + "\n".join("%d. %s" % (i + 1, c["line"]) for i, c in enumerate(chosen))
        ok, why = decide.send_qq(runner, qq, msg[:MAX_CHARS])
        for c in chosen:
            rec = {"key": c["key"], "kind": c["kind"], "text": c["text"], "sent": ok}
            if not ok:
                rec["error"] = why
            out.append(rec)
            if not ok:
                continue
            st = state.get(c["key"])
            if c["kind"] == "new":
                state[c["key"]] = {"first_at": _iso(now), "last_sent_at": _iso(now), "sends": 1, "text": c["text"],
                                   "resolved_at": None}
            elif c["kind"] == "remind":
                st["sends"] = int(st.get("sends") or 0) + 1
                st["last_sent_at"], st["text"] = _iso(now), c["text"]
            else:
                st["resolved_at"] = _iso(now)
    cut = now - _dt.timedelta(seconds=KEEP_RESOLVED_S)
    for key in [k for k, v in state.items() if v.get("resolved_at") and (core.parse_iso(v["resolved_at"]) or now) < cut]:
        del state[key]
    save(state)
    return out
