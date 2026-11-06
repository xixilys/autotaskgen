"""progress-qq: Hermes plugin. Owner DM replies to decision cards go to ~/.progress/inbox.jsonl.

Grammar (full-width spaces/digits ok, lowercase d ok):
  D<n> 选 <k> | D<n> 推翻[ 理由] | D<n> (为什么|解释)[ ...]   and a bare `选 <k>` when exactly one card is open.
Config: ~/.progress/qq.json {"target","owner"}; open cards: ~/.progress/qq_cards.json (kept by patrol's decide.py).
Never raises; anything that is not a reply returns None (normal Hermes behaviour).
"""
import asyncio
import json
import os
import re
import subprocess
import time
import unicodedata

LABEL = "com.example.progress-patrol"
_PICK = r"(?:选|選)\s*(\d{1,2})"
_FULL = re.compile(r"^D(\d+)\s*(?:" + _PICK + r"|(推翻|推翻了)(?:\s+(.*))?|(为什么|為什麼|解释|解釋)(?:\s+(.*))?)\s*$",
                   re.S)
_BARE = re.compile(r"^" + _PICK + r"\s*$")


def _home():
    return os.path.expanduser(os.environ.get("PROGRESS_HOME") or "~/.progress")


def _norm(text):
    text = unicodedata.normalize("NFKC", str(text or "")).strip()  # full-width digits/spaces/letters -> ASCII
    return re.sub(r"^[dD](?=\d)", "D", text)


def parse_reply(text, open_cards):
    """-> {"card","verb": pick|overrule|why,"arg"} or None. `open_cards`: iterable of card ids still awaiting a pick."""
    t = _norm(text)
    if not t:
        return None
    m = _FULL.match(t)
    if m:
        card = "D" + m.group(1)
        if m.group(2):
            return {"card": card, "verb": "pick", "arg": m.group(2)}
        if m.group(3):
            return {"card": card, "verb": "overrule", "arg": (m.group(4) or "").strip()}
        return {"card": card, "verb": "why", "arg": (m.group(6) or "").strip()}
    b = _BARE.match(t)
    if b:
        cards = list(open_cards or [])
        if len(cards) == 1:
            return {"card": cards[0], "verb": "pick", "arg": b.group(1)}
    return None


def _load(name):
    try:
        with open(os.path.join(_home(), name), encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def open_cards():
    return sorted(c for c, v in _load("qq_cards.json").items() if isinstance(v, dict) and v.get("status") in ("escalated", "panel_failed"))


def _seen(message_id):
    if not message_id:
        return False
    try:
        with open(os.path.join(_home(), "inbox.jsonl"), encoding="utf-8") as fh:
            for raw in fh:
                try:
                    if json.loads(raw).get("message_id") == message_id:
                        return True
                except ValueError:
                    continue
    except OSError:
        pass
    return False


def _append(entry):
    os.makedirs(_home(), exist_ok=True)
    with open(os.path.join(_home(), "inbox.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _kick():
    try:
        subprocess.Popen(["launchctl", "kickstart", "gui/%d/%s" % (os.getuid(), LABEL)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _ack(gateway, source, text):
    try:
        adapter = gateway.adapters[source.platform]
        res = adapter.send(source.chat_id, text)
        if asyncio.iscoroutine(res):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop:
                loop.create_task(res)
            else:
                asyncio.run(res)
    except Exception:
        pass


def _is_qq_dm(source, owner):
    plat = getattr(source, "platform", None)
    plat = getattr(plat, "value", plat)
    return (str(plat).lower() == "qqbot" and str(getattr(source, "chat_type", "")).lower() == "dm"
            and bool(owner) and str(getattr(source, "user_id", "")) == owner)


def handler(event, gateway=None, session_store=None, **kw):
    try:
        source = getattr(event, "source", None)
        owner = str(_load("qq.json").get("owner") or "")
        if source is None or not _is_qq_dm(source, owner):
            return None
        hit = parse_reply(getattr(event, "text", ""), open_cards())
        if not hit:
            return None
        mid = getattr(event, "message_id", None)
        if not _seen(mid):
            _append(dict(hit, at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), text=str(event.text)[:500],
                         message_id=mid))
            _ack(gateway, source, "收到 %s %s，处理中" % (hit["card"], {"pick": "选 " + hit["arg"], "overrule": "推翻", "why": "为什么"}[hit["verb"]]))
            _kick()
        return {"action": "skip", "reason": "progress-qq"}
    except Exception:
        return None


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", handler)
