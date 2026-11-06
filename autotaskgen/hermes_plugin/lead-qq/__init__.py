"""lead-qq: Hermes plugin. Owner DMs addressed to the project lead go to the lead's inbox and wake it (docs/LEAD.md).

Matches (NFKC-normalised): text starting with one of `prefixes` (default 负责人 / @负责人 / lead, any case), or
<ref_prefix><n> followed by anything (default L: "L3 推翻 理由", "L3 为什么", "L3 同意").
Owner = <owner_conf> "owner" (default <PROGRESS_HOME>/qq.json), on `platform` (default qqbot) DMs only.
Anything else returns None (normal Hermes behaviour). Never raises.

Config, all optional: <LEAD_ROOT>/lead-qq.json (LEAD_ROOT default ~/.lead; or the path in LEAD_QQ_CONFIG)
  {"project": "<pid>", "prefixes": ["负责人", "@负责人", "lead"], "ref_prefix": "L",
   "wake_label": "com.example.lead-wake", "wake_cmd": null, "owner_conf": "~/.progress/qq.json",
   "platform": "qqbot", "ack_text": "负责人收到（{id}），几分钟内处理，结果在 QQ 回你"}
Environment overrides: LEAD_PROJECT, LEAD_WAKE_LABEL. Without "project" the only <LEAD_ROOT>/<pid>/config.json is used.
Inbox rows: {"id": "I<n>", "dir": "in", "at", "text", "ref", "message_id"}; duplicates by message_id are dropped.
"""
import asyncio
import glob
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

DEFAULTS = {
    "project": "",
    "prefixes": ["负责人", "@负责人", "lead"],
    "ref_prefix": "L",
    "wake_label": "com.example.lead-wake",
    "wake_cmd": None,      # argv list run instead of `launchctl kickstart` (e.g. on Linux)
    "owner_conf": "",
    "platform": "qqbot",
    "ack_text": "负责人收到（{id}），几分钟内处理，结果在 QQ 回你",
}


def _root():
    return os.path.expanduser(os.environ.get("LEAD_ROOT") or "~/.lead")


def config():
    path = os.path.expanduser(os.environ.get("LEAD_QQ_CONFIG") or os.path.join(_root(), "lead-qq.json"))
    conf = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if isinstance(doc, dict):
            conf.update({k: v for k, v in doc.items() if v is not None})
    except (OSError, ValueError):
        pass
    if os.environ.get("LEAD_PROJECT"):
        conf["project"] = os.environ["LEAD_PROJECT"]
    if os.environ.get("LEAD_WAKE_LABEL"):
        conf["wake_label"] = os.environ["LEAD_WAKE_LABEL"]
    if not conf.get("owner_conf"):
        conf["owner_conf"] = os.path.join(os.environ.get("PROGRESS_HOME") or "~/.progress", "qq.json")
    return conf


def lead_home(conf):
    pid = conf.get("project")
    if not pid:
        found = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(_root(), "*", "config.json")))
        pid = found[0] if len(found) == 1 else ""
    if not pid or "/" in pid or pid.startswith("."):
        return None
    return os.path.join(_root(), pid)


def _norm(text):
    return unicodedata.normalize("NFKC", str(text or "")).strip()


def _head_re(prefixes):
    alts = []
    for p in sorted({_norm(x) for x in prefixes or [] if _norm(x)}, key=len, reverse=True):
        alts.append(re.escape(p) + (r"\b" if p[-1].isascii() and p[-1].isalnum() else ""))
    return re.compile(r"^(?:%s)[\s:：,，]*(.*)$" % "|".join(alts), re.S | re.I) if alts else None


def parse(text, prefixes=None, ref_prefix=None):
    """-> {"ref": "L3"|None, "text"} or None. Defaults: DEFAULTS prefixes / ref_prefix."""
    t = _norm(text)
    if not t:
        return None
    rp = _norm(DEFAULTS["ref_prefix"] if ref_prefix is None else ref_prefix)
    if rp:
        m = re.match(r"^%s(\d+)(?!\d)\s*(.*)$" % re.escape(rp), t, re.S | re.I)
        if m:
            return {"ref": rp.upper() + m.group(1), "text": t}
    head = _head_re(DEFAULTS["prefixes"] if prefixes is None else prefixes)
    m = head.match(t) if head else None
    if m and m.group(1).strip():
        return {"ref": None, "text": m.group(1).strip()}
    return None


def _owner(conf):
    try:
        with open(os.path.expanduser(conf["owner_conf"]), encoding="utf-8") as fh:
            return str(json.load(fh).get("owner") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _rows(home):
    out = []
    try:
        with open(os.path.join(home, "inbox.jsonl"), encoding="utf-8") as fh:
            for raw in fh:
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def append(home, entry):
    """Append an inbound row with the next I<n> id; False if this message_id is already there."""
    os.makedirs(home, exist_ok=True)
    rows = _rows(home)
    if entry.get("message_id") and any(r.get("message_id") == entry["message_id"] for r in rows):
        return False
    n = max([int(str(r.get("id"))[1:]) for r in rows
             if str(r.get("id", "")).startswith("I") and str(r.get("id"))[1:].isdigit()] or [0])
    entry["id"] = "I%d" % (n + 1)
    with open(os.path.join(home, "inbox.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return True


def _kick(conf):
    try:
        cmd = conf.get("wake_cmd")
        if isinstance(cmd, list) and cmd:
            argv = [os.path.expanduser(str(x)) for x in cmd]
        elif sys.platform == "darwin" and conf.get("wake_label"):
            argv = ["launchctl", "kickstart", "gui/%d/%s" % (os.getuid(), conf["wake_label"])]
        else:
            return
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _ack(gateway, source, text):
    try:
        res = gateway.adapters[source.platform].send(source.chat_id, text)
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


def is_owner_dm(source, owner, platform="qqbot"):
    plat = getattr(source, "platform", None)
    plat = getattr(plat, "value", plat)
    return (str(plat).lower() == str(platform).lower() and str(getattr(source, "chat_type", "")).lower() == "dm"
            and bool(owner) and str(getattr(source, "user_id", "")) == owner)


def handler(event, gateway=None, session_store=None, **kw):
    try:
        conf = config()
        source = getattr(event, "source", None)
        if source is None or not is_owner_dm(source, _owner(conf), conf.get("platform") or "qqbot"):
            return None
        hit = parse(getattr(event, "text", ""), conf.get("prefixes"), conf.get("ref_prefix"))
        home = lead_home(conf)
        if not hit or not home:
            return None
        entry = {"dir": "in", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "text": hit["text"][:2000],
                 "ref": hit["ref"], "message_id": getattr(event, "message_id", None)}
        if append(home, entry):
            try:
                ack = str(conf.get("ack_text") or "").format(id=entry["id"])
            except (KeyError, IndexError, ValueError):
                ack = str(conf.get("ack_text") or "")
            if ack:
                _ack(gateway, source, ack)
            _kick(conf)
        return {"action": "skip", "reason": "lead-qq"}
    except Exception:
        return None


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", handler)
