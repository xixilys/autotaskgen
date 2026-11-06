/* Progress board frontend. Plain JS + SVG, no build step. All URLs are relative so the
 * board works behind a path prefix. */
(function () {
  "use strict";

  var LIVE_STALE_S = 30 * 60;
  var QUIET_S = 2 * 3600;
  var SVGNS = "http://www.w3.org/2000/svg";

  var STATUS = {
    planned: "已排好", ready: "可以派", running: "在跑", delivered: "已交付", reviewed: "已审过",
    blocked: "卡住", failed: "失败", superseded: "被取代", cancelled: "已取消", unknown: "状态未知"
  };
  var STATUS_ORDER = ["planned", "ready", "running", "delivered", "reviewed", "blocked", "failed",
    "superseded", "cancelled", "unknown"];
  var OUTCOME = { PASS: "通过", PARTIAL: "部分通过", FAIL: "没通过", UNKNOWN: "结果不明" };
  var BLOCKER = { dep: "等依赖", design: "设计没定", env: "环境问题", resource: "缺资源", other: "其他原因" };
  var MS_STATUS = { open: "未达成", claimed: "已报，等协调人确认", confirmed: "已确认达成" };
  var SCREEN = { stream_error: "末屏在报错", stuck: "末屏像卡住", waiting_input: "末屏在等输入" };
  var ROLE = { router: "router", worker: "worker", coordinator: "协调人", patrol: "巡检", "import": "导入", human: "人" };
  var KIND = {
    line_open: ["开主线", "flag"], line_close: ["关主线", "flag"], project_update: ["改项目声明", "edit"],
    node_plan: ["排好节点", "plus"], node_dispatch: ["派出", "send"], node_heartbeat: ["心跳", "pulse"],
    node_deliver: ["交付", "box"], node_review: ["审查", "eye"], node_block: ["卡住", "pause"],
    node_supersede: ["被取代", "swap"], node_cancel: ["取消", "x"],
    milestone_claim: ["报关键节点达成", "star"], milestone_confirm: ["确认关键节点", "check"],
    milestone_reject: ["驳回关键节点", "x"], nudge: ["巡检催促", "bell"], auto_dispatch: ["巡检代派", "bolt"],
    observe: ["巡检发现", "scan"], graph_propose: ["拆出子图", "plus"], worker_release: ["释放 worker 终端", "x"],
    decision: ["裁决", "check"], human_reply: ["用户回复", "bell"]
  };
  var ICON = {
    flag: "M5 21V4M5 4h11l-2 4 2 4H5", edit: "M4 20h4L19 9l-4-4L4 16v4z", plus: "M12 5v14M5 12h14",
    send: "M4 12l16-8-6 16-3-7-7-1z", pulse: "M3 12h4l2-5 4 10 2-5h6", box: "M4 8l8-4 8 4v8l-8 4-8-4V8zM4 8l8 4 8-4M12 12v8",
    eye: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
    pause: "M8 5v14M16 5v14", swap: "M4 8h13l-3-3M20 16H7l3 3", x: "M6 6l12 12M18 6L6 18",
    star: "M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z",
    check: "M5 12l5 5 9-10", bell: "M6 16V11a6 6 0 0 1 12 0v5l2 2H4l2-2zM10 21h4", bolt: "M13 2L4 14h7l-1 8 9-12h-7z",
    scan: "M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4M4 12h16",
    chev: "M9 6l6 6-6 6", back: "M15 6l-6 6 6 6", fork: "M6 4v6a4 4 0 0 0 4 4h4M6 4v16M18 10a4 4 0 1 1 0 8 4 4 0 0 1 0-8z",
    layers: "M12 3l9 5-9 5-9-5 9-5zM3 13l9 5 9-5"
  };
  var KIND_COLOR = {
    node_deliver: "var(--st-delivered)", node_review: "var(--st-reviewed)", node_block: "var(--st-blocked)",
    node_dispatch: "var(--st-running)", milestone_claim: "var(--ms)", milestone_confirm: "var(--ms)",
    milestone_reject: "var(--st-failed)", nudge: "var(--patrol-nudge)", auto_dispatch: "var(--patrol-auto)",
    node_cancel: "var(--muted)", node_supersede: "var(--muted)", decision: "var(--ms)", human_reply: "var(--ms)"
  };
  var DECISION = { agreed: "面板一致，已执行", escalated: "面板未一致，等用户", panel_failed: "面板没跑完，等用户",
    overruled: "用户推翻", human: "用户裁决" };
  var ATTRIB = { terminal: "按终端对上", worktree: "按工作区对上", task: "按任务对上", run: "按 orca run 对上" };
  function coreNameOf(st, id) {
    var c = ((st && st.project && st.project.cores) || []).filter(function (x) { return x.id === id; })[0];
    return (c && c.name) || id || "";
  }
  // ---- router tree helpers (live.router_tree)
  var RKIND = { core: "核心协调人", line: "主线", sub: "子 router" };
  function sameH(a, b) { return !!a && !!b && (a === b || a.indexOf(b) === 0 || b.indexOf(a) === 0); }
  function treeOf(live) { return (live && Array.isArray(live.router_tree) ? live.router_tree : []).filter(function (n) { return n && n.handle; }); }
  function routerNode(live, h) { return treeOf(live).filter(function (n) { return sameH(n.handle, h); })[0] || null; }
  function routerChain(live, h) {
    var out = [], n = routerNode(live, h), guard = 0;
    while (n && guard++ < 50) { out.unshift(n); n = n.parent ? routerNode(live, n.parent) : null; }
    return out;
  }
  function routerName(live, h) { var n = routerNode(live, h); return n ? n.name : (h ? h.slice(0, 13) : "未知 router"); }
  function routerLabel(live, h) { var n = routerNode(live, h); return routerName(live, h) + (n ? "（" + (RKIND[n.kind] || n.kind) + "）" : ""); }
  function chainText(live, h) { return routerChain(live, h).map(function (n) { return n.name; }).join(" › "); }
  function sender(live, w) { return w.router ? "派出者：" + routerLabel(live, w.router) : ""; }

  var TIERS = [
    { id: "sol", name: "Sol", test: /sol/i },
    { id: "terra", name: "Terra", test: /terra/i },
    { id: "luna", name: "Luna", test: /luna/i },
    { id: "flash", name: "Flash", test: /flash|deepseek/i },
    { id: "claude", name: "Claude", test: /claude|opus|sonnet|haiku/i }
  ];

  var app = document.getElementById("app");
  var crumbs = document.getElementById("crumbs");
  var tip = document.getElementById("tip");
  var links = document.getElementById("links");
  var drawer = document.getElementById("drawer");
  var drawerBody = document.getElementById("drawer-body");
  var scrim = document.getElementById("scrim");
  var selbar = document.getElementById("selbar");
  var conn = document.getElementById("conn");

  var cache = { projects: null, states: {}, fetchedAt: {}, sup: {}, prj: {}, lead: {} };
  var view = { name: null, ids: [] };
  var selected = loadSel();
  var seenFeed = {};

  // ----------------------------------------------------------- utilities
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function ms(iso) { var t = iso ? Date.parse(iso) : NaN; return isNaN(t) ? null : t; }
  function rel(iso) {
    var t = ms(iso);
    if (t == null) return "未知";
    var s = Math.round((Date.now() - t) / 1000);
    if (s < 0) s = 0;
    if (s < 60) return "刚刚";
    if (s < 3600) return Math.floor(s / 60) + " 分钟前";
    if (s < 86400) return Math.floor(s / 3600) + " 小时前";
    return Math.floor(s / 86400) + " 天前";
  }
  function dur(sec) {
    if (sec == null || isNaN(sec)) return "未知";
    if (sec < 3600) return Math.max(1, Math.round(sec / 60)) + " 分";
    if (sec < 86400) return (sec / 3600).toFixed(sec < 36000 ? 1 : 0).replace(/\.0$/, "") + " 小时";
    return (sec / 86400).toFixed(1).replace(/\.0$/, "") + " 天";
  }
  function durShort(sec) {
    if (sec == null || isNaN(sec)) return "?";
    if (sec < 3600) return Math.max(1, Math.round(sec / 60)) + "m";
    return Math.round(sec / 3600) + "h";
  }
  function abs(iso) {
    var t = ms(iso);
    if (t == null) return "未知";
    var d = new Date(t);
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }
  // absolute local time plus relative, e.g. "11-04 18:20（1 小时前）"
  function when(iso) { return ms(iso) == null ? "未知" : abs(iso) + "（" + rel(iso) + "）"; }
  // a deadline: "还有 2 小时" before it, "已过 30 分钟" after it
  function untilTxt(iso) {
    var t = ms(iso);
    if (t == null) return "未知";
    var s = Math.round((t - Date.now()) / 1000), a = Math.abs(s);
    var d = a < 3600 ? Math.max(1, Math.floor(a / 60)) + " 分钟" : a < 86400 ? Math.floor(a / 3600) + " 小时" : Math.floor(a / 86400) + " 天";
    return s >= 0 ? "还有 " + d : "已过 " + d;
  }
  function timeTag(iso, prefix) {
    return '<span class="chip">' + esc(prefix || "") + esc(when(iso)) + "</span>";
  }
  // orca task records the other side adds to live.json: live.tasks[taskId] = {state, dispatched_at, completed_at}
  var NO_TIME = "导入记录，无实际时间";
  var TASK_STATE = { succeeded: "成功", failed: "失败", ready: "待派", running: "在跑", dispatched: "已派出" };
  function taskOf(st, n) {
    var tasks = st && st.live && st.live.tasks;
    var id = n && n.refs && n.refs.task;
    return tasks && typeof tasks === "object" && id && tasks[id] ? tasks[id] : null;
  }
  // real dispatch/finish times; imported nodes only carry import time in their events, so use the task record
  function nodeStamps(st, n) {
    var tk = taskOf(st, n);
    if (n.imported) {
      var d = (tk && tk.dispatched_at) || null, c = (tk && tk.completed_at) || null;
      return { disp: d, done: c, none: !d && !c, task: tk };
    }
    return { disp: n.dispatched_at || null, done: n.delivered_at || null, none: false, task: tk };
  }
  function nodeSortTime(st, n) {
    var sp = nodeStamps(st, n);
    return ms(sp.disp) != null ? ms(sp.disp) : ms(sp.done) != null ? ms(sp.done) : n.imported ? null : ms(n.planned_at || n.updated_at);
  }
  function nodeTimeText(st, n) {
    var sp = nodeStamps(st, n);
    if (sp.none) return NO_TIME;
    if (!n.imported) return "更新于 " + when(n.updated_at);
    return [sp.disp ? "派出 " + when(sp.disp) : "", sp.done ? "结束 " + when(sp.done) : ""].filter(Boolean).join(" · ");
  }
  function tierOf(model) {
    if (!model) return { id: "other", name: "未知档" };
    for (var i = 0; i < TIERS.length; i++) if (TIERS[i].test.test(model)) return TIERS[i];
    return { id: "other", name: "其他" };
  }
  function tierColor(id) { return "var(--tier-" + id + ")"; }
  function textW(s, px) {
    var w = 0;
    for (var i = 0; i < s.length; i++) w += s.charCodeAt(i) > 0x2e80 ? px : px * 0.58;
    return w;
  }
  function wrap(s, maxPx, px, maxLines) {
    s = String(s || "");
    var lines = [], cur = "";
    for (var i = 0; i < s.length; i++) {
      if (textW(cur + s[i], px) > maxPx) { lines.push(cur); cur = s[i]; } else cur += s[i];
    }
    if (cur) lines.push(cur);
    if (lines.length > maxLines) {
      lines = lines.slice(0, maxLines);
      var last = lines[maxLines - 1];
      while (last.length && textW(last + "…", px) > maxPx) last = last.slice(0, -1);
      lines[maxLines - 1] = last + "…";
    }
    return lines;
  }
  function hashStr(s) { var h = 2166136261; for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
  function loadSel() { try { return JSON.parse(localStorage.getItem("pb-sel") || "[]") || []; } catch (e) { return []; } }
  function saveSel() { try { localStorage.setItem("pb-sel", JSON.stringify(selected)); } catch (e) { /* private mode */ } }
  function getJSON(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) {
      return r.json().catch(function () { return { ok: false, code: "bad_json", error: "服务返回的不是 JSON" }; })
        .then(function (j) { if (!r.ok) { j = j || {}; j.httpStatus = r.status; throw j; } return j; });
    });
  }

  // live freshness measured with the server's age plus local elapsed time (no clock skew)
  function liveInfo(st) {
    if (!st) return { stale: true, at: null, age: null };
    var elapsed = (Date.now() - (st._fetched || Date.now())) / 1000;
    var age = st.live_age_s == null ? null : st.live_age_s + elapsed;
    var stale = st.live_stale || !st.live || age == null || age > LIVE_STALE_S;
    return { stale: stale, at: st.live_at, age: age, live: stale ? null : st.live, reason: st.live_reason };
  }
  function staleBadge(info) {
    if (!info.at) return '<span class="badge-unknown">在跑数据未知</span>';
    return '<span class="badge-stale">过期 · ' + esc(when(info.at)) + "</span>";
  }

  function topoMilestones(list) {
    var byId = {}, order = [], placed = {};
    list.forEach(function (m) { byId[m.id] = m; });
    var guard = 0;
    while (order.length < list.length && guard++ < list.length + 2) {
      list.forEach(function (m) {
        if (placed[m.id]) return;
        var ok = (m.after || []).every(function (a) { return placed[a] || !byId[a]; });
        if (ok) { placed[m.id] = true; order.push(m); }
      });
    }
    list.forEach(function (m) { if (!placed[m.id]) order.push(m); });
    return order;
  }

  // ----------------------------------------------------------- shapes
  function beadShape(status, cx, cy, r) {
    var c = 'cx="' + cx + '" cy="' + cy + '"';
    switch (status) {
      case "planned": return '<circle class="shape s-planned" ' + c + ' r="' + (r - 1) + '"/>';
      case "ready": return '<circle class="shape s-ready" ' + c + ' r="' + (r - 1) + '"/><circle class="s-ready-in" ' + c + ' r="' + (r * 0.36) + '"/>';
      case "running": return '<circle class="s-running-ring" ' + c + ' r="' + r + '"/><circle class="shape s-running" ' + c + ' r="' + r + '"/>' +
        '<path d="M' + (cx - r * 0.28) + " " + (cy - r * 0.42) + "l" + (r * 0.7) + " " + (r * 0.42) + "l" + (-r * 0.7) + " " + (r * 0.42) + 'z" fill="#fff"/>';
      case "delivered": return '<circle class="shape s-delivered" ' + c + ' r="' + r + '"/><rect x="' + (cx - r * 0.32) + '" y="' + (cy - r * 0.32) + '" width="' + (r * 0.64) + '" height="' + (r * 0.64) + '" rx="1" fill="#fff"/>';
      case "reviewed": return '<circle class="shape s-reviewed" ' + c + ' r="' + r + '"/><path class="s-tick" d="M' + (cx - r * 0.45) + " " + cy + "l" + (r * 0.32) + " " + (r * 0.34) + "l" + (r * 0.6) + " " + (-r * 0.66) + '"/>';
      case "blocked": return '<rect class="shape s-blocked" x="' + (cx - r) + '" y="' + (cy - r) + '" width="' + 2 * r + '" height="' + 2 * r + '" rx="' + r * 0.35 + '"/>' +
        '<path class="s-bar" d="M' + (cx - r * 0.3) + " " + (cy - r * 0.4) + "v" + (r * 0.8) + "M" + (cx + r * 0.3) + " " + (cy - r * 0.4) + "v" + (r * 0.8) + '"/>';
      case "failed": return '<path class="shape s-failed" d="M' + cx + " " + (cy - r * 1.15) + "L" + (cx + r * 1.15) + " " + cy + "L" + cx + " " + (cy + r * 1.15) + "L" + (cx - r * 1.15) + " " + cy + 'z"/>' +
        '<path class="s-x" d="M' + (cx - r * 0.35) + " " + (cy - r * 0.35) + "l" + r * 0.7 + " " + r * 0.7 + "M" + (cx + r * 0.35) + " " + (cy - r * 0.35) + "l" + (-r * 0.7) + " " + r * 0.7 + '"/>';
      case "superseded": return '<circle class="shape s-superseded" ' + c + ' r="' + (r * 0.55) + '"/>';
      case "cancelled": return '<circle class="shape s-cancelled" ' + c + ' r="' + (r * 0.6) + '"/><path class="s-slash" d="M' + (cx - r * 0.42) + " " + (cy + r * 0.42) + "L" + (cx + r * 0.42) + " " + (cy - r * 0.42) + '"/>';
      default: return '<circle class="shape s-unknown" ' + c + ' r="' + (r - 1) + '"/><text class="s-q" x="' + cx + '" y="' + (cy + 3) + '">?</text>';
    }
  }
  function beadIcon(status, size) {
    size = size || 18;
    var r = size * 0.36;
    return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 ' + size + " " + size + '" aria-hidden="true">' + beadShape(status, size / 2, size / 2, r) + "</svg>";
  }
  function nodeStatus(n) { return n.status_unknown && n.status === "planned" ? "unknown" : n.status; }
  function stationIcon(status, size) {
    size = size || 18; var c = size / 2, r = size * 0.36;
    var cls = status === "confirmed" ? "st-confirmed" : status === "claimed" ? "st-claimed" : "st-open";
    var tick = status === "confirmed" ? '<path class="tick" d="M' + (c - r * 0.45) + " " + c + "l" + r * 0.32 + " " + r * 0.34 + "l" + r * 0.6 + " " + (-r * 0.66) + '"/>' : "";
    return '<svg class="station" width="' + size + '" height="' + size + '" viewBox="0 0 ' + size + " " + size + '" aria-hidden="true"><circle class="' + cls + '" cx="' + c + '" cy="' + c + '" r="' + r + '"/>' + tick + "</svg>";
  }
  function icon(name) { return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="' + ICON[name] + '"/></svg>'; }

  // ----------------------------------------------------------- goal ring
  function ringSvg(msList, size, prog) {
    var S = 148, cx = 74, R = 66, r = 47;
    var n = msList.length;
    var out = '<svg class="ring" viewBox="0 0 ' + S + " " + S + '" role="img" aria-label="' +
      esc(prog ? (prog.confirmed + " / " + prog.total + " 个关键节点已确认，" + prog.claimed + " 个待确认") : "") + '">';
    if (!n) {
      out += '<circle class="seg-open" cx="74" cy="74" r="66" stroke-dasharray="3 5"/><circle class="seg-open" cx="74" cy="74" r="47" stroke-dasharray="3 5"/>';
      out += '<text class="lbl" x="74" y="78" text-anchor="middle">未声明关键节点</text></svg>';
      return out;
    }
    var gap = n > 1 ? 0.07 : 0;
    function pt(rad, a) { return (cx + rad * Math.cos(a)).toFixed(2) + " " + (cx + rad * Math.sin(a)).toFixed(2); }
    msList.forEach(function (m, i) {
      var a0 = -Math.PI / 2 + (2 * Math.PI * i) / n + gap / 2;
      var a1 = -Math.PI / 2 + (2 * Math.PI * (i + 1)) / n - gap / 2;
      if (n === 1) a1 = a0 + 2 * Math.PI - 0.0001;
      var big = a1 - a0 > Math.PI ? 1 : 0;
      var d = "M" + pt(R, a0) + " A" + R + " " + R + " 0 " + big + " 1 " + pt(R, a1) + " L" + pt(r, a1) +
        " A" + r + " " + r + " 0 " + big + " 0 " + pt(r, a0) + " Z";
      var cls = m.status === "confirmed" ? "seg-confirmed" : m.status === "claimed" ? "seg-claimed" : "seg-open";
      out += '<path class="' + cls + '" d="' + d + '"><title>' + esc(m.id + " " + (m.title || "") + "：" + (MS_STATUS[m.status] || "未知")) + "</title></path>";
    });
    var done = prog ? prog.confirmed : msList.filter(function (m) { return m.status === "confirmed"; }).length;
    out += '<text x="74" y="80" text-anchor="middle"><tspan class="num">' + done + '</tspan><tspan class="den" dx="4">/ ' + n + "</tspan></text>";
    out += '<text class="lbl" x="74" y="100" text-anchor="middle">关键节点</text></svg>';
    return out;
  }

  // ----------------------------------------------------------- legends
  function legendNodes() {
    return '<div class="legend" aria-label="节点状态图例">' + STATUS_ORDER.map(function (s) {
      return '<span class="it">' + beadIcon(s, 16) + esc(STATUS[s]) + "</span>";
    }).join("") + '<span class="it"><svg width="16" height="16" aria-hidden="true"><circle class="imp" cx="8" cy="8" r="7"/></svg>虚线外圈 = 从旧记录导入</span></div>';
  }
  function legendStations() {
    return '<div class="legend">' + ["confirmed", "claimed", "open"].map(function (s) {
      return '<span class="it">' + stationIcon(s, 16) + esc(MS_STATUS[s]) + "</span>";
    }).join("") + "</div>";
  }
  function legendTiers(extra) {
    return '<div class="legend" aria-label="模型档位图例">' + TIERS.concat([{ id: "other", name: "其他/未知" }]).map(function (t) {
      return '<span class="it"><i class="wdot" style="color:' + tierColor(t.id) + '"></i>' + esc(t.name) + "</span>";
    }).join("") + (extra || "") + "</div>";
  }

  // ----------------------------------------------------------- routing
  function route() {
    var h = (location.hash || "#/").slice(1);
    var m;
    hideTip(); clearLinks();
    if ((m = h.match(/^\/p\/([^/]+)\/(core|line|r)\/(.+)$/))) return show("project", [decodeURIComponent(m[1])], { t: m[2], k: decodeURIComponent(m[3]) });
    if ((m = h.match(/^\/p\/([^/]+)$/))) return show("project", [decodeURIComponent(m[1])]);
    if ((m = h.match(/^\/cmp\/(.+)$/))) return show("compare", m[1].split(",").map(decodeURIComponent).filter(Boolean));
    return show("home", []);
  }
  function show(name, ids, sub) {
    var skey = sub ? sub.t + ":" + sub.k : "";
    var changed = view.name !== name || view.ids.join() !== ids.join() || (view.skey || "") !== skey;
    view = { name: name, ids: ids, sub: sub || null, skey: skey };
    if (changed) { closeDrawer(); window.scrollTo(0, 0); app.innerHTML = '<div class="empty">载入中…</div>'; }
    refresh();
  }
  var refreshing = null, again = false;
  function refresh() {
    if (refreshing) { again = true; return refreshing; }
    var v = view, p;
    if (v.name === "home") p = getJSON("api/projects").then(function (j) { cache.projects = j; });
    else p = Promise.all(v.ids.map(function (id) {
      return getJSON("api/p/" + encodeURIComponent(id) + "/state").then(function (j) {
        j._fetched = Date.now(); cache.states[id] = j;
      }).catch(function (e) { cache.states[id] = { _error: e }; }).then(function () {
        if (v.ids.length !== 1) return;
        return getJSON("api/p/" + encodeURIComponent(id) + "/supervisor").then(function (j) { cache.sup[id] = j; })
          .catch(function () { cache.sup[id] = null; })
          .then(function () { return getJSON("api/p/" + encodeURIComponent(id) + "/project_review"); })
          .then(function (j) { cache.prj[id] = j; }, function () { cache.prj[id] = null; })
          .then(function () { return getJSON("api/p/" + encodeURIComponent(id) + "/lead"); })
          .then(function (j) { cache.lead[id] = j; }, function () { cache.lead[id] = null; });
      });
    }));
    refreshing = p.then(function () { if (v === view) render(); }).catch(function (e) {
      if (v === view) app.innerHTML = errBox(e);
    }).then(function () {
      refreshing = null;
      if (again) { again = false; refresh(); }
    });
    return refreshing;
  }
  function errBox(e) {
    var msg = (e && (e.error || e.message)) || "服务没有响应";
    var title = e && e.code === "no_project" ? "没有这个项目" : "读取失败";
    return '<div class="err-box panel" style="padding:24px"><b>' + esc(title) + "</b>" + esc(msg) + '<p><a href="#/">回到总览</a></p></div>';
  }
  function render() {
    var keepScroll = saveLaneScroll();
    if (view.name === "home") renderHome();
    else if (view.name === "project") renderProject();
    else renderCompare();
    restoreLaneScroll(keepScroll);
    renderSelbar();
    snapOpen();
  }
  // screenshot helper: ?snap=1&open=<line>|<node> or ?open=<milestone> opens a drawer once
  var snapOpened = false;
  function snapOpen() {
    var so = /[?&]snap=1/.test(location.search) && /[?&]open=([^&]+)/.exec(location.search);
    var st = cache.states[view.ids[0]];
    if (!so || snapOpened || !st || st._error) return;
    snapOpened = true;
    var parts = decodeURIComponent(so[1]).split("|");
    drawerState = st;
    if (parts.length === 2) nodeDrawer(st, parts[0], parts[1]); else msDrawer(st, parts[0]);
  }
  function saveLaneScroll() {
    var out = {};
    app.querySelectorAll("[data-scroll-key]").forEach(function (el) { out[el.getAttribute("data-scroll-key")] = el.scrollLeft; });
    return out;
  }
  function restoreLaneScroll(saved) {
    app.querySelectorAll("[data-scroll-key]").forEach(function (el) {
      var k = el.getAttribute("data-scroll-key");
      if (saved[k] != null) el.scrollLeft = saved[k];
      else if (el.hasAttribute("data-scroll-end")) el.scrollLeft = el.scrollWidth;
    });
  }

  // ----------------------------------------------------------- home
  function renderHome() {
    crumbs.innerHTML = "<span>项目总览</span>";
    var j = cache.projects;
    var list = (j && j.projects) || [];
    var html = '<div class="home-h"><h1>所有项目</h1><p>' + (list.length ? list.length + " 个项目 · 点卡片进入，勾选多张可并排看" : "") + "</p></div>";
    if (!list.length) {
      html += '<div class="panel empty">还没有注册的项目。用 <code>progress project-init</code> 注册一个。</div>';
      app.innerHTML = html; return;
    }
    html += '<div class="cards">' + list.map(cardHtml).join("") + "</div>";
    html += '<div class="panel" style="margin-top:18px">' + legendStations() + legendTiers('<span class="it"><i class="wdot unattr" style="color:var(--tier-other)"></i>空心 = 没对上节点</span>') + "</div>";
    app.innerHTML = html;
  }
  function cardHtml(p) {
    if (p.error) {
      return '<div class="panel card" data-open="' + esc(p.id) + '" tabindex="0"><div></div><div><h3>' + esc(p.id) + '</h3><p class="goal">读不出这个项目：' + esc(p.error.error || p.error.code) + "</p></div></div>";
    }
    var msl = topoMilestones(p.milestones || []);
    var sel = selected.indexOf(p.id) >= 0;
    var pr = p.progress || {};
    var meta = '<span class="chip">' + esc(p.lines) + " 条主线</span>";
    if (pr.claimed) meta += '<span class="chip" style="color:var(--ms)">' + pr.claimed + " 个等确认</span>";
    meta += timeTag(p.last_event_at, "事件 ");
    var dots;
    if (p.live_stale || p.workers == null) {
      dots = '<span class="lab">在跑</span>' + (p.live_at ? '<span class="badge-stale" title="最后快照 ' + esc(when(p.live_at)) + '">过期</span>' : '<span class="badge-unknown">未知</span>');
    } else {
      // 在跑 = screen busy and Orca task not settled (server sets phase); older servers send no phase: count all
      var ws = p.workers.filter(function (w) { return !w.phase || w.phase === "running"; }).sort(function (a, b) {
        return TIERS.indexOf(tierOf(a.model)) - TIERS.indexOf(tierOf(b.model));
      });
      var ling = p.workers.filter(function (w) { return w.phase === "lingering"; }).length;
      var idle = p.workers.filter(function (w) { return w.phase === "idle"; }).length;
      dots = '<span class="lab">在跑 ' + ws.length + "</span>" + ws.map(function (w) {
        var t = tierOf(w.model);
        return '<i class="wdot' + (w.line ? "" : " unattr") + '" style="color:' + tierColor(t.id) + '" title="' + esc(t.name + (w.line ? " · " + w.line : w.router ? " · 由 router 派出" : " · 未归属") + " · " + (w.host || "未知机器")) + '"></i>';
      }).join("") +
        (idle ? '<span class="lab" title="任务没结束，但屏幕空闲或状态不明">停着 ' + idle + "</span>" : "") +
        (ling ? '<span class="lab" title="任务已结束、终端还开着，不算在跑">已结束仍在 ' + ling + "</span>" : "");
    }
    return '<div class="panel card' + (sel ? " sel" : "") + '" data-open="' + esc(p.id) + '" tabindex="0" role="link" aria-label="打开 ' + esc(p.name) + '">' +
      '<button class="sel-btn" type="button" data-sel="' + esc(p.id) + '" aria-pressed="' + sel + '" aria-label="选中以并排查看"><svg width="14" height="14" viewBox="0 0 24 24"><path d="M5 12l5 5 9-10"/></svg></button>' +
      ringSvg(msl, 148, pr) +
      '<div style="min-width:0"><h3>' + esc(p.name || p.id) + '</h3><p class="goal">' + esc((p.final_goal && p.final_goal.text) || "最终目标未写") + "</p>" +
      '<div class="meta">' + meta + '</div><div class="dots-row">' + dots + "</div></div></div>";
  }
  function renderSelbar() {
    if (view.name !== "home" || !selected.length) { selbar.hidden = true; return; }
    selbar.hidden = false;
    selbar.innerHTML = "<span>已选 " + selected.length + " 个项目</span>" +
      '<button type="button" class="ghost" data-act="clear-sel">清空</button>' +
      '<button type="button" data-act="cmp"' + (selected.length < 1 ? " disabled" : "") + ">" + (selected.length > 1 ? "并排查看" : "打开") + "</button>";
  }

  // ----------------------------------------------------------- project
  function renderProject() {
    var id = view.ids[0];
    var st = cache.states[id];
    crumbs.innerHTML = '<a href="#/">项目总览</a><span>/</span><span>' + esc(st && st.project ? st.project.name : id) + "</span>";
    if (!st) return;
    if (st._error) { app.innerHTML = errBox(st._error); return; }
    if (view.sub) {
      var li0 = liveInfo(st), o0 = routerModel(st, li0).byKey[routerKey(view.sub)];
      crumbs.innerHTML = '<a href="#/">项目总览</a><span>/</span><a href="#/p/' + encodeURIComponent(id) + '">' + esc(st.project.name) + "</a><span>/</span><span>" + esc(o0 ? crumbName(o0) : "router") + "</span>";
      app.innerHTML = routerPageHtml(st, routerKey(view.sub));
    } else app.innerHTML = projectHtml(st, false);
    drawAll(app, st);
  }
  function renderCompare() {
    crumbs.innerHTML = '<a href="#/">项目总览</a><span>/</span><span>并排查看 ' + view.ids.length + " 个</span>";
    var html = '<div class="cmp">';
    view.ids.forEach(function (id) {
      var st = cache.states[id];
      if (!st) html += '<div class="col"><div class="panel empty">载入中…</div></div>';
      else if (st._error) html += '<div class="col">' + errBox(st._error) + "</div>";
      else html += '<div class="col" data-pid="' + esc(id) + '">' + projectHtml(st, true) + "</div>";
    });
    app.innerHTML = html + "</div>";
    view.ids.forEach(function (id) {
      var el = app.querySelector('[data-pid="' + cssEsc(id) + '"]');
      if (el && cache.states[id] && !cache.states[id]._error) drawAll(el, cache.states[id]);
    });
  }
  function dwHtml(v) { return Array.isArray(v) ? v.map(function (x) { return esc(x); }).join("<br>") : esc(v == null || v === "" ? "未知" : v); }
  function cssEsc(s) { return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&"); }

  // ---- supervisor (巡检员) report
  var SUP_KIND = { stalled: "该动没动", looping: "原地打转、反复打回", hallucinating: "说的结果和证据对不上", drifting: "偏离目标", idle_parallelism: "有能并行的活没派",
    no_progress: "长时间没有验收过的产出", waiting_human: "在等人", no_next_step: "没有下一步", dispatch_broken: "派活失败", mislabeled: "卡点标错了",
    metric_breach: "硬指标超线", followup_overdue: "上次的建议到期没落实", review_heavy: "审查花的时间偏多", stale_state: "状态记录没跟上",
    graph_gap: "依赖关系没画进图", other: "其他" };
  function supKind(k) { return SUP_KIND[k] || "其他"; }
  // older reports carry the supervisor's internal words; new ones are asked for plain language (supervisor PROMPT 七)
  function plain(t) {
    return String(t == null ? "" : t).replace(/\s*\bfu_[0-9a-f]{6,}\b/g, "").replace(/地图/g, "巡检汇总").replace(/\bSTATE\b/g, "状态记录")
      .replace(/\btooling\b/g, "框架").replace(/blocked\/human/g, "等人").replace(/审查\/执行(指标)?\s*([0-9.]+)/g, function (_, __, x) {
        return "审查时间是执行时间的 " + Math.round(parseFloat(x) * 100) + "%";
      });
  }
  var SUP_ST = { problem: ["problem", "✕ 有问题"], attention: ["attention", "▲ 需关注"], ok: ["ok", "● 正常"] };
  var SUP_DELIV = { sent: "已发给 router", rate_limited: "限流未发", unchanged: "内容未变", dry_run: "未发（预览）", router_absent: "router 不在线未发", policy_violation: "越权，未发", failed: "发送失败", forwarded_to_coordinator: "router 不在，已转给协调人" };
  var PRJ_V = { ok: ["ok", "● 正常"], warn: ["attention", "▲ 需关注"], problem: ["problem", "✕ 有问题"] };
  function prjHtml(pid) {
    var j = cache.prj && cache.prj[pid];
    var h = '<section class="panel sup prj" aria-label="项目级"><div class="panel-h"><h2>项目级</h2>';
    if (!j || j.empty || !j.generated_at) return h + '<span class="sp"></span></div><div class="sup-body sup-none">还没有项目级档案</div></section>';
    var v = PRJ_V[j.verdict_hint] || PRJ_V.ok;
    h += timeTag(j.generated_at, "档案 ") + '<b class="sup-st ' + v[0] + '">' + esc(v[1]) + '</b><span class="sp"></span></div><div class="sup-body">';
    var rows = j.stage_progress || [];
    if (rows.length) h += '<ul class="sup-list">' + rows.map(function (r) {
      return '<li class="sup-ln"><div class="sup-lh"><span class="idb">' + esc(r.stage) + "</span><span>已满足 " + esc(r.met) + "/" + esc(r.total) +
        "，最近 24h 关闭 " + esc(r.closed_24h) + "</span></div></li>";
    }).join("") + "</ul>";
    var fl = j.flags || [];
    if (fl.length) h += '<ul class="sup-list">' + fl.map(function (f) {
      var c = f.level === "problem" ? "problem" : "attention";
      return '<li class="sup-ln ' + c + '"><div class="sup-note">' + esc(f.text) + "</div></li>";
    }).join("") + "</ul>";
    else h += '<div class="sup-ok">没有标记</div>';
    if ((j.pending || []).length) h += '<div class="sup-iss">待处理调整 ' + j.pending.length + " 条</div>";
    h += '<div class="sup-iss">档案文件：<code>' + esc(j.md_path || j.md_file || j.file || "") + "</code></div>";
    return h + "</div></section>";
  }
  var LEAD_V = { ok: ["ok", "● 正常"], on_track: ["ok", "● 正常"], warn: ["attention", "▲ 需关注"], attention: ["attention", "▲ 需关注"], at_risk: ["attention", "▲ 有风险"],
    problem: ["problem", "✕ 有问题"], off_track: ["problem", "✕ 有问题"] };
  var LEAD_ST = { open: "待落实", escalated: "两次没落实，已上报给你", pending: "待落实", not_done: "回查没落实" };
  var LEAD_KIND = { adjust: "调整", question: "提问", tooling: "框架问题", ask_human: "问你", escalate: "上报" };
  var LEAD_SHOW = 5;
  function leadRow(r) {
    var due = r.check_after && ms(r.check_after) != null && ms(r.check_after) < Date.now();
    var kind = LEAD_KIND[r.kind] ? LEAD_KIND[r.kind] + " · " : "";
    return '<li class="sup-ln' + (due || r.status === "escalated" ? " attention" : "") + '"><div class="sup-lh"><span class="idb">' + esc(r.id || "") + "</span>" +
      "<span>" + esc(kind) + "交给 <b>" + esc(r.to_label || r.to || "未写收件人") + "</b></span>" +
      '<span class="chip">' + esc(LEAD_ST[r.status] || r.status || "待落实") + "</span>" +
      (r.check_after ? '<span class="chip">' + esc((due ? "该回查了：" : "回查：") + abs(r.check_after) + "（" + untilTxt(r.check_after) + "）") + "</span>" : "") + "</div>" +
      '<div class="sup-note">' + esc(r.text || "（负责人没写内容）") + "</div>" +
      (r.done_when ? '<div class="sup-iss"><b>怎样算做到</b>：' + dwHtml(r.done_when) + "</div>" : "") + "</li>";
  }
  function leadHtml(pid) {
    var j = (cache.lead && cache.lead[pid]) || (cache.states[pid] && cache.states[pid].lead);
    var h = '<section class="panel sup lead" aria-label="项目负责人"><div class="panel-h"><h2>项目负责人</h2>';
    if (!j || j.empty) return h + '<span class="sp"></span></div><div class="sup-body sup-none">项目负责人还没有写状态</div></section>';
    var v = LEAD_V[String(j.verdict || "").toLowerCase()] || ["attention", String(j.verdict || "未给结论")];
    var c = j.ledger_counts || {}, rows = j.open || [];
    h += timeTag(j.updated, "最近一轮 ") + (j.cycle != null ? '<span class="chip">第 ' + esc(j.cycle) + " 轮</span>" : "") +
      '<b class="sup-st ' + v[0] + '">' + esc(v[1]) + '</b><span class="sp"></span></div><div class="sup-body">';
    h += '<div class="sup-intro">项目负责人是替你盯整个项目的 AI 会话，每隔几小时醒一次：看进度，直接给各线下调整，过一段时间再回去检查做到没有。' +
      "这里列的是它派出去、还没确认做到的事：交给谁、做什么、怎样算做到、什么时候回查。两次回查都没做到会上报给你。你不用操作；不同意哪条，在 QQ 发「负责人 L编号 推翻 理由」。</div>";
    if (j.summary) h += '<div class="sup-note"><b>本轮</b>：' + esc(j.summary) + "</div>";
    h += '<div class="sup-iss">待落实 ' + (c.open != null ? c.open : rows.length) + " 条 · 已做到 " + (c.done || 0) + " 条 · 已上报给你 " + (c.escalated || 0) +
      " 条 · 已撤回 " + (c.obsolete || 0) + " 条</div>";
    if (rows.length) {
      h += '<ul class="sup-list">' + rows.slice(0, LEAD_SHOW).map(leadRow).join("") + "</ul>";
      if (rows.length > LEAD_SHOW) h += '<details class="lead-more"><summary>还有 ' + (rows.length - LEAD_SHOW) + " 条（回查时间更晚）</summary>" +
        '<ul class="sup-list">' + rows.slice(LEAD_SHOW).map(leadRow).join("") + "</ul></details>";
    } else h += '<div class="sup-ok">派出去的事都已做到</div>';
    if (j.report) h += '<div class="sup-iss">本轮完整报告：<code>' + esc(j.report) + "</code></div>";
    return h + "</div></section>";
  }
  function supData(pid) {
    var j = cache.sup && cache.sup[pid];
    if (!j || !j.report || !Array.isArray(j.report.lines)) return { report: null, stale: true };
    return { report: j.report, stale: !!j.stale };
  }
  function supLine(sd, id) {
    if (!sd.report || sd.stale) return null;
    for (var i = 0; i < sd.report.lines.length; i++) {
      var l = sd.report.lines[i];
      if (l && l.line === id && (l.status === "problem" || l.status === "attention")) return l;
    }
    return null;
  }
  function supBadge(l) {
    if (!l) return "";
    var d = SUP_ST[l.status], iss = (l.issues || [])[0];
    var tip = (l.progress_note || "") + (iss ? "\n" + supKind(iss.kind) + "：" + plain(iss.evidence) : "");
    return '<span class="sup-b ' + d[0] + '" title="' + esc(tip) + '">' + esc(d[1]) + "</span>";
  }
  function supHtml(pid) {
    var sd = supData(pid), r = sd.report;
    var h = '<section class="panel sup" aria-label="巡检员"><div class="panel-h"><h2>巡检员</h2>';
    if (!r) return h + '<span class="sp"></span></div><div class="sup-body sup-none">巡检员还没有出报告</div></section>';
    var rank = { problem: 0, attention: 1 };
    var bad = r.lines.filter(function (l) { return l && rank[l.status] != null; })
      .sort(function (a, b) { return rank[a.status] - rank[b.status]; });
    var dl = {};
    (r.delivery || []).forEach(function (d) { if (d && d.line) dl[d.line] = d; });
    h += timeTag(r.generated_at, "巡检 ") + '<span class="chip">' + (bad.length ? bad.length + " 条需处理" : "各线正常") + "</span>" +
      (sd.stale ? '<span class="badge-stale">巡检员数据过期</span>' : "") + '<span class="sp"></span></div>' +
      '<div class="sup-body' + (sd.stale ? " is-stale" : "") + '">';
    if (Array.isArray(r.policy_violation) && r.policy_violation.length) h += '<div class="sup-viol" role="alert">巡检员越权：本轮运行了写操作命令，建议已全部不发送。' +
      r.policy_violation.slice(0, 3).map(function (v) { return "<code>" + esc(String(v.command || "").slice(0, 120)) + "</code>"; }).join(" ") + "</div>";
    h += '<div class="sup-intro">巡检员是每 15 分钟自动跑一次的检查程序：读各条线的记录，只把需要处理的线列在下面。每条写了哪里不对、依据是什么、给 router 的建议，以及建议有没有发出去。</div>';
    if (r.summary) h += '<p class="sup-sum" id="sup-sum">' + esc(plain(r.summary)) + '</p><button type="button" class="sup-more" data-sup-more aria-controls="sup-sum" aria-expanded="false">展开</button>';
    if (!bad.length) h += '<div class="sup-ok">各线正常</div>';
    else h += '<ul class="sup-list">' + bad.map(function (l) {
      var d = SUP_ST[l.status], dv = dl[l.line];
      return '<li class="sup-ln ' + d[0] + '"><div class="sup-lh"><span class="idb">' + esc(l.line) + '</span><b class="sup-st ' + d[0] + '">' + esc(d[1]) + "</b>" +
        (dv ? '<span class="sup-dv">' + esc(SUP_DELIV[dv.reason] || dv.reason || "") + "</span>" : "") + "</div>" +
        (l.progress_note ? '<div class="sup-note">' + esc(plain(l.progress_note)) + "</div>" : "") +
        (l.issues || []).map(function (i) { return '<div class="sup-iss"><b>' + esc(supKind(i.kind)) + "</b>：" + esc(plain(i.evidence)) + "</div>"; }).join("") +
        ((l.suggestions || []).length ? '<div class="sup-iss">建议：</div><ul class="sup-sg">' + l.suggestions.slice(0, 3).map(function (x) { return "<li>" + esc(plain(x)) + "</li>"; }).join("") + "</ul>" : "") + "</li>";
    }).join("") + "</ul>";
    return h + "</div></section>";
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-sup-more]");
    if (!b) return;
    var p = document.getElementById("sup-sum"), open = p.classList.toggle("open");
    b.textContent = open ? "收起" : "展开"; b.setAttribute("aria-expanded", open ? "true" : "false");
  });
  // top banner: disk level (red critical / amber low), absent and rebound routers
  function alertBanner(st, li) {
    var lv = li.live;
    if (!lv) return "";
    var rows = [];
    var res = lv.resources || {};
    Object.keys(res).forEach(function (h) {
      (res[h].disks || []).forEach(function (d) {
        if (d.level !== "critical" && d.level !== "low") return;
        rows.push('<div class="ab-row ' + (d.level === "critical" ? "crit" : "warn") + '"><b>' + (d.level === "critical" ? "磁盘严重不足" : "磁盘偏低") + "</b> " +
          esc((h === "wsl" ? "WSL" : h) + " " + d.mount + " 只剩 " + d.avail_gb + " G（已用 " + d.used_pct + "%）") + "</div>");
      });
    });
    var routers = lv.routers || {};
    Object.keys(routers).forEach(function (k) {
      var r = routers[k];
      if (r.present === false) rows.push('<div class="ab-row warn"><b>' + esc(k) + " router 不在线</b> 终端 " + esc(r.handle || "") + " 已消失，巡检已通知协调人</div>");
      if (r.rebound_from) rows.push('<div class="ab-row info"><b>' + esc(k) + " router 已换成新终端</b> project.json 待更新（旧 " + esc(r.rebound_from) + "，新 " + esc(r.handle || "") + "）</div>");
    });
    return rows.length ? '<div class="alert-banner" role="alert">' + rows.join("") + "</div>" : "";
  }
  function projectHtml(st, compact) {
    var doc = st.project, S = st.state, pid = doc.id;
    var msl = msMerged(st);
    var li = liveInfo(st);
    var lastAt = S.feed && S.feed.length ? S.feed[0].at : null;
    var nodesTotal = 0, running = 0;
    Object.keys(S.lines).forEach(function (k) { var c = S.lines[k].counts || {}; running += c.running || 0; nodesTotal += Object.keys(S.lines[k].nodes).length; });
    var head = alertBanner(st, li) + '<div class="proj-h">' + ringSvg(msl, 108, S.progress) + '<div style="min-width:0">' +
      (compact ? '<h1><a href="#/p/' + encodeURIComponent(pid) + '" style="color:inherit;text-decoration:none">' + esc(doc.name) + "</a></h1>" : "<h1>" + esc(doc.name) + "</h1>") +
      '<p class="goal">' + esc((doc.final_goal && doc.final_goal.text) || "最终目标未写") + '</p><div class="meta">' +
      '<span class="chip">' + (doc.lines || []).length + " 条主线 · " + nodesTotal + " 个节点 · " + running + " 个节点在跑</span>" +
      timeTag(lastAt, "最近事件 ") + (li.stale ? staleBadge(li) : timeTag(li.at, "在跑快照 ")) + "</div></div></div>";
    var track = '<section class="panel" aria-label="目标轨道"><div class="panel-h"><h2>目标轨道</h2>' +
      '<span class="chip">' + S.progress.confirmed + " 已确认 · " + S.progress.claimed + " 等确认 · " + (S.progress.total - S.progress.confirmed - S.progress.claimed) + " 未达成</span>" +
      '<span class="sp"></span>' + timeTag(lastAt, "数据 ") + '</div><div class="track-wrap" data-track data-scroll-key="track-' + esc(pid) + '"></div>' + legendStations() + "</section>";
    var M = routerModel(st, li);
    var lanes = '<section class="panel" aria-label="主线泳道"><div class="panel-h"><h2>主线泳道</h2><span class="chip">点一行进入该 router 的完整节点图</span><span class="sp"></span>' +
      (li.stale ? staleBadge(li) : hostsHtml(M, li) + timeTag(li.at, "快照 ")) + "</div>" + lanesHtml(st, li) + legendTiers('<span class="it"><span class="wchip ling demo" style="--tc:var(--tier-other)"><i></i><b>虚线</b></span>任务已结束、进程还在</span>') + "</section>";
    var feed = '<section class="panel" aria-label="实时动态"><div class="panel-h"><h2>实时动态</h2><span class="sp"></span>' + timeTag(lastAt, "最新 ") + "</div>" + feedHtml(st, compact ? 12 : 80) + "</section>";
    var sup = compact ? "" : supHtml(pid);
    if (compact) return head + '<div class="col">' + track + lanes + feed + "</div>";
    return head + sup + leadHtml(pid) + prjHtml(pid) + '<div class="layout"><div class="col">' + track + lanes + '</div><div class="feed-col">' + feed + "</div></div>";
  }
  function msMerged(st) {
    var S = st.state;
    return topoMilestones((st.project.milestones || []).map(function (m) {
      var s = S.milestones[m.id] || {};
      return Object.assign({}, m, { status: s.status || "open", fold: s });
    }));
  }

  function drawAll(root, st) {
    var tw = root.querySelector("[data-track]");
    if (tw) drawTrack(tw, st);
    root.querySelectorAll("[data-feed-id]").forEach(function (li) {
      var id = li.getAttribute("data-feed-id");
      if (seenFeed[id] === false) li.classList.add("new");
    });
    Object.keys(seenFeed).forEach(function (k) { seenFeed[k] = true; });
  }

  // ---- goal track: one band per core, milestones laid out as a DAG (column = longest path over `after`)
  function trackLayout(msl, st) {
    var byId = {}, depth = {}, busy = {};
    msl.forEach(function (m) { byId[m.id] = m; });
    function dep(m) {
      if (depth[m.id] != null) return depth[m.id];
      if (busy[m.id]) return 0;
      busy[m.id] = true;
      var d = 0;
      (m.after || []).forEach(function (a) { if (byId[a]) d = Math.max(d, dep(byId[a]) + 1); });
      busy[m.id] = false;
      return (depth[m.id] = d);
    }
    msl.forEach(dep);
    var cores = ((st.project.cores) || []).map(function (c) { return { id: c.id, name: c.name || c.id }; });
    var known = {};
    cores.forEach(function (c) { known[c.id] = true; });
    if (msl.some(function (m) { return !known[m.core]; })) cores.push({ id: null, name: "其他" });
    var maxD = 0, pos = {}, bands = [];
    cores.forEach(function (c) {
      var mine = msl.filter(function (m) { return c.id == null ? !known[m.core] : m.core === c.id; });
      if (!mine.length) return;
      var cols = {}, rows = 1;
      mine.forEach(function (m) { (cols[depth[m.id]] = cols[depth[m.id]] || []).push(m); });
      Object.keys(cols).map(Number).sort(function (a, b) { return a - b; }).forEach(function (d) {
        maxD = Math.max(maxD, d);
        var col = cols[d];
        // keep a milestone near its upstream ones to reduce crossings
        var key = {};
        col.forEach(function (m, i) {
          var ys = (m.after || []).map(function (a) { return pos[a] && pos[a].row; }).filter(function (v) { return v != null; });
          key[m.id] = ys.length ? ys.reduce(function (a, b) { return a + b; }, 0) / ys.length : 0.5 + i * 0.01;
        });
        col.sort(function (a, b) { return key[a.id] - key[b.id]; });
        col.forEach(function (m, i) { pos[m.id] = { col: d, row: i }; });
        rows = Math.max(rows, col.length);
      });
      bands.push({ core: c, ms: mine, rows: rows });
    });
    var hasDown = {};
    msl.forEach(function (m) { (m.after || []).forEach(function (a) { hasDown[a] = true; }); });
    return { bands: bands, pos: pos, maxD: maxD, byId: byId, sinks: msl.filter(function (m) { return !hasDown[m.id]; }) };
  }
  function drawTrack(wrapEl, st) {
    var msl = msMerged(st);
    var W = Math.max(280, wrapEl.clientWidth - 8);
    var fg = st.project.final_goal || {};
    var reached = msl.length > 0 && msl.every(function (m) { return m.status === "confirmed"; });
    var lay = trackLayout(msl, st);
    wrapEl.innerHTML = W < 560 ? trackVertical(lay, W, fg, reached) : trackHorizontal(lay, W, fg, reached);
  }
  function stationG(m, x, y) {
    var cls = m.status === "confirmed" ? "st-confirmed" : m.status === "claimed" ? "st-claimed" : "st-open";
    var g = '<g class="station" data-ms="' + esc(m.id) + '" tabindex="0" role="button" aria-label="' + esc(m.id + " " + (m.title || "") + "，" + MS_STATUS[m.status]) + '">';
    g += '<circle class="halo" cx="' + x + '" cy="' + y + '" r="22"/>';
    if (m.status === "claimed") g += '<circle class="wait-ring" cx="' + x + '" cy="' + y + '" r="12"/>';
    g += '<circle class="' + cls + '" cx="' + x + '" cy="' + y + '" r="12"/>';
    if (m.status === "confirmed") g += '<path class="tick" d="M' + (x - 5.5) + " " + y + "l3.8 4 7-8" + '"/>';
    return g;
  }
  function edgePath(x1, y1, x2, y2) {
    if (Math.abs(y1 - y2) < 1) return "M" + x1 + " " + y1 + "H" + x2;
    var mx = (x1 + x2) / 2;
    return "M" + x1 + " " + y1 + " C" + mx + " " + y1 + " " + mx + " " + y2 + " " + x2 + " " + y2;
  }
  function trackHorizontal(lay, W, fg, reached) {
    var x0 = 64, padR = 96, rowH = 118, topPad = 78, botPad = 82, gap = 10;
    var s = Math.max(132, (W - x0 - padR) / (lay.maxD + 2));
    var xEnd = x0 + (lay.maxD + 1) * s, width = Math.max(W, xEnd + padR);
    var y = 4, P = {};
    lay.bands.forEach(function (b) {
      b.top = y; b.h = topPad + (b.rows - 1) * rowH + botPad; y += b.h + gap;
    });
    var H = y - gap + 4;
    lay.bands.forEach(function (b) {
      b.ms.forEach(function (m) { var p = lay.pos[m.id]; P[m.id] = { x: x0 + p.col * s, y: b.top + topPad + p.row * rowH }; });
    });
    var yEnd = H / 2;
    var out = '<svg class="track-svg" width="' + width + '" height="' + H + '" viewBox="0 0 ' + width + " " + H + '" role="group" aria-label="关键节点轨道">';
    lay.bands.forEach(function (b) {
      out += '<rect class="band" x="4" y="' + b.top + '" width="' + (width - 8) + '" height="' + b.h + '" rx="10"/><text class="band-l" x="16" y="' + (b.top + 20) + '">' + esc(b.core.name) + "</text>";
    });
    // one edge per `after` link (cross-core links included); solid when the upstream is confirmed
    lay.bands.forEach(function (b) {
      b.ms.forEach(function (m) {
        (m.after || []).forEach(function (a) {
          if (!P[a]) return;
          out += '<path class="edge' + (lay.byId[a].status === "confirmed" ? " done" : "") + '" d="' + edgePath(P[a].x + 13, P[a].y, P[m.id].x - 13, P[m.id].y) + '"/>';
        });
      });
    });
    lay.sinks.forEach(function (m) {
      out += '<path class="edge' + (m.status === "confirmed" ? " done" : "") + '" d="' + edgePath(P[m.id].x + 13, P[m.id].y, xEnd - 22, yEnd) + '"/>';
    });
    lay.bands.forEach(function (b) {
      b.ms.forEach(function (m) {
        var x = P[m.id].x, yy = P[m.id].y;
        out += stationG(m, x, yy);
        if (m.status === "claimed") {
          out += '<rect class="wait-pill" x="' + (x - 44) + '" y="' + (yy - 46) + '" width="88" height="20" rx="10"/><text class="wait-txt" x="' + x + '" y="' + (yy - 32) + '">等协调人确认</text>';
        }
        out += '<text class="sid" x="' + x + '" y="' + (yy + 32) + '">' + esc(m.id) + "</text>";
        wrap(m.title || "", s - 18, 12.5, 2).forEach(function (ln, k) {
          out += '<text class="stitle" x="' + x + '" y="' + (yy + 50 + k * 16) + '">' + esc(ln) + "</text>";
        });
        out += "</g>";
      });
    });
    out += '<g class="station terminus' + (reached ? " reached" : "") + '" data-goal="1" tabindex="0" role="button" aria-label="最终目标">' +
      '<circle class="halo" cx="' + xEnd + '" cy="' + yEnd + '" r="24"/>' +
      '<rect class="flag" x="' + (xEnd - 15) + '" y="' + (yEnd - 15) + '" width="30" height="30" rx="7" transform="rotate(45 ' + xEnd + " " + yEnd + ')"/>' +
      '<circle class="flag-in" cx="' + xEnd + '" cy="' + yEnd + '" r="6"/>' +
      '<text class="sid" x="' + xEnd + '" y="' + (yEnd + 36) + '">终点</text>';
    wrap(fg.text || "最终目标", padR + 40, 12.5, 3).forEach(function (ln, k) {
      out += '<text class="gtitle" x="' + Math.min(xEnd, width - 70) + '" y="' + (yEnd + 54 + k * 16) + '">' + esc(ln) + "</text>";
    });
    return out + "</g></svg>";
  }
  function trackVertical(lay, W, fg, reached) {
    var rowH = 62, x = 30, y = 8, items = "", rail = "";
    lay.bands.forEach(function (b) {
      items += '<text class="band-l" x="6" y="' + (y + 12) + '">' + esc(b.core.name) + "</text>";
      y += 26;
      b.ms.slice().sort(function (p, q) { return lay.pos[p.id].col - lay.pos[q.id].col || lay.pos[p.id].row - lay.pos[q.id].row; }).forEach(function (m) {
        var cy = y + 14;
        items += stationG(m, x, cy);
        items += '<text class="sid" x="' + (x + 26) + '" y="' + (cy - 4) + '" style="text-anchor:start">' + esc(m.id) + "</text>";
        if (m.status === "claimed") items += '<rect class="wait-pill" x="' + (x + 56) + '" y="' + (cy - 18) + '" width="88" height="19" rx="9.5"/><text class="wait-txt" x="' + (x + 100) + '" y="' + (cy - 5) + '">等协调人确认</text>';
        var t = wrap(m.title || "", W - x - 40, 12.5, 1)[0] || "";
        items += '<text class="stitle" x="' + (x + 26) + '" y="' + (cy + 12) + '" style="text-anchor:start">' + esc(t) + "</text>";
        if ((m.after || []).length) items += '<text class="after-t" x="' + (x + 26) + '" y="' + (cy + 26) + '">前置：' + esc(m.after.join("、")) + "</text>";
        items += "</g>";
        y += rowH;
      });
      rail += "";
    });
    var yEnd = y + 14, H = yEnd + 40;
    var out = '<svg class="track-svg" width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + " " + H + '" role="group" aria-label="关键节点轨道">' + items;
    out += '<g class="station terminus' + (reached ? " reached" : "") + '" data-goal="1" tabindex="0" role="button" aria-label="最终目标"><circle class="halo" cx="' + x + '" cy="' + yEnd + '" r="22"/>' +
      '<rect class="flag" x="' + (x - 13) + '" y="' + (yEnd - 13) + '" width="26" height="26" rx="6" transform="rotate(45 ' + x + " " + yEnd + ')"/><circle class="flag-in" cx="' + x + '" cy="' + yEnd + '" r="5"/>' +
      '<text class="sid" x="' + (x + 26) + '" y="' + (yEnd - 4) + '" style="text-anchor:start">终点</text>';
    out += '<text class="gtitle" x="' + (x + 26) + '" y="' + (yEnd + 14) + '" style="text-anchor:start">' + esc(wrap(fg.text || "最终目标", W - x - 40, 12.5, 1)[0]) + "</text></g>";
    return out + "</svg>";
  }

  // ---- swimlanes: overview (core header + one compact row per line) and drill-in router pages
  function lampOf(st, ln, ls, li) {
    var counts = (ls && ls.counts) || {};
    if ((counts.blocked || 0) >= 2) return { c: "red", why: (counts.blocked) + " 个节点卡住" };
    if (li.stale) return { c: "grey", why: li.at ? "在跑数据过期，看不出负责人是否在线" : "没有在跑数据" };
    var r = (li.live.routers || {})[ln.id];
    if (!r || r.present == null) return { c: "grey", why: "快照里没有这条主线的负责人终端" };
    if (r.present === false) return { c: "red", why: "负责人终端不在线" };
    var last = ms(ls && ls.last_event_at);
    if (last != null && Date.now() - last <= QUIET_S * 1000) return { c: "green", why: "负责人在线，2 小时内有事件" };
    return { c: "yellow", why: last == null ? "负责人在线，但还没有任何事件" : "负责人在线，但 " + rel(ls.last_event_at) + "起没有新事件" };
  }
  function values(o) { return Object.keys(o || {}).map(function (k) { return o[k]; }); }
  function wkey(w) { return String(w.workspace || w.pid); }
  // "混合评估器（ACC）：早先的…" -> ["混合评估器", "早先的…"]; the id badge carries the id
  function splitName(name, id) {
    var s = String(name || id || ""), i = s.indexOf("：");
    var a = i > 0 ? s.slice(0, i) : s, b = i > 0 ? s.slice(i + 1).trim() : "";
    if (id) a = a.replace("（" + id + "）", "").replace("(" + id + ")", "").trim() || a;
    return [a, b];
  }
  function coreShort(name) { return String(name || "").split(/\s+/)[0] || String(name || ""); }
  function modelShort(m) {
    if (!m) return "未知";
    var x = /opus|sonnet|haiku/i.exec(m);
    if (x) return x[0].charAt(0).toUpperCase() + x[0].slice(1).toLowerCase();
    var t = tierOf(m);
    return t.id !== "other" ? t.name : String(m).split("/").pop().slice(0, 10);
  }
  function hspan(sec) {
    if (sec == null || isNaN(sec)) return "?";
    if (sec < 3600) return Math.max(1, Math.round(sec / 60)) + "m";
    if (sec < 36000) return (sec / 3600).toFixed(1).replace(/\.0$/, "") + "h";
    if (sec < 172800) return Math.round(sec / 3600) + "h";
    return Math.round(sec / 86400) + "d";
  }
  function wElapsed(w) {
    var now = Date.now();
    if (w.lingering) {
      var c = w.task ? ms(w.task.completed_at) : null;
      return { txt: "已结束" + (c != null ? " " + hspan((now - c) / 1000) : ""), long: "任务已结束" + (c != null ? " " + dur((now - c) / 1000) : "") + "，进程还在" };
    }
    var s = ms(w.started_at);
    return { txt: s == null ? "?" : hspan((now - s) / 1000), long: s == null ? "开始时间未知" : "已跑 " + dur((now - s) / 1000) };
  }
  // output activity from the snapshot's terminal list: "在输出" / "N 分钟前有输出" / "空闲 N 小时", null when unknown
  function wActivity(w) {
    if (!w) return null;
    if (w.busy === true) return "在输出";
    var lo = ms(w.last_output_at);
    if (lo == null) return null;
    var sec = Math.max(0, (Date.now() - lo) / 1000);
    return sec < 3600 ? Math.max(1, Math.round(sec / 60)) + " 分钟前有输出" : "空闲 " + dur(sec);
  }
  function reportTail(n) {
    var r = n && n.refs && n.refs.report;
    return r ? String(r).split("/").slice(-2).join("/") : null;
  }
  // stalled-worker state from the snapshot's screen read (error / idle / exited), null when fine
  function wStall(w) {
    var sc = w && w.screen;
    if (!sc || !sc.flag || w.lingering) return null;
    var since = ms(sc.since), mins = since == null ? null : Math.max(1, Math.round((Date.now() - since) / 60000));
    if (sc.flag === "exited") return { cls: "bad", txt: "进程已退出", long: "进程已退出", line: sc.line };
    if (sc.flag === "error") return { cls: "bad", txt: "报错", long: "屏幕上有报错" + (mins ? "，已 " + mins + " 分钟没动" : ""), line: sc.line };
    if (sc.flag === "idle") return { cls: "warn", txt: "已停 " + (mins || "?") + " 分钟", long: "停在输入框" + (mins ? " " + mins + " 分钟" : ""), line: sc.line };
    return null;
  }
  function isReview(n) { return !!n && (n.kind === "review" || n.kind === "check"); }
  var REVIEW_TAG = '<span class="ktag" title="审查节点：只读和判断，不重跑重活">审查</span>';
  function sortWorkers(ws) {
    return ws.slice().sort(function (a, b) {
      if (!!a.lingering !== !!b.lingering) return a.lingering ? 1 : -1;
      return (ms(a.started_at) || 0) - (ms(b.started_at) || 0);
    });
  }

  // Router model: cores -> declared lines -> sub-routers (any depth), live workers attached to the
  // deepest router that owns them. Rebuilt per fetched state.
  function routerModel(st, li) {
    if (st._rm && st._rm.stale === li.stale) return st._rm.m;
    var doc = st.project, live = li.live, tree = live ? treeOf(live) : [];
    var M = { byKey: {}, list: [], unattr: [], orphans: [], byNode: {}, workers: [] };
    function mk(key, o) { o.key = key; o.kids = []; o.workers = []; o.parentKey = null; M.byKey[key] = o; M.list.push(o); return o; }
    (doc.cores || []).forEach(function (c) {
      var tn = tree.filter(function (n) { return n.kind === "core" && (n.core === c.id || sameH(n.handle, c.coordinator)); })[0] || null;
      mk("core:" + c.id, { type: "core", id: c.id, name: c.name || c.id, handle: c.coordinator || (tn && tn.handle), tn: tn, core: c.id, line: null });
    });
    (doc.lines || []).forEach(function (l) {
      var tn = tree.filter(function (n) { return n.kind === "line" && n.line === l.id; })[0] || null;
      mk("line:" + l.id, { type: "line", id: l.id, name: l.name || l.id, handle: l.router || (tn && tn.handle), tn: tn, core: l.core, line: l.id, decl: l });
    });
    tree.forEach(function (n) {
      if (n.kind !== "sub" || M.byKey["r:" + n.handle]) return;
      mk("r:" + n.handle, { type: "sub", id: n.handle, name: n.name || n.handle.slice(0, 13), handle: n.handle, tn: n, core: n.core, line: n.line });
    });
    function byHandle(h) {
      if (!h) return null;
      for (var i = 0; i < M.list.length; i++) if (sameH(M.list[i].handle, h)) return M.list[i];
      return null;
    }
    M.byHandle = byHandle;
    M.list.forEach(function (o) {
      if (o.type === "core") return;
      var p = null;
      if (o.type === "line") {
        var dp = o.decl.parent;
        if (dp) p = M.byKey["line:" + dp] || M.byKey["core:" + dp] || null;
        if (!p && o.tn && o.tn.parent) { p = byHandle(o.tn.parent); if (p && p.type === "sub") p = null; }
        if (!p) p = M.byKey["core:" + o.core] || null;
      } else {
        p = byHandle(o.tn.parent);
        if (!p) p = (o.line && M.byKey["line:" + o.line]) || (o.core && M.byKey["core:" + o.core]) || null;
      }
      for (var q = p, g = 0; q && g < 64; g++) { if (q === o) { p = null; break; } q = q.parentKey ? M.byKey[q.parentKey] : null; }
      if (p && p !== o) { o.parentKey = p.key; p.kids.push(o); } else M.orphans.push(o);
    });
    var workers = live ? (live.workers || []).filter(function (w) { return w && typeof w === "object"; }) : [];
    workers.forEach(function (w) {
      var r = byHandle(w.router), o = null;
      if (r && r.type === "sub") o = r;
      if (!o && w.line && M.byKey["line:" + w.line]) o = M.byKey["line:" + w.line];
      if (!o && r) o = r;
      if (!o && w.core && M.byKey["core:" + w.core]) o = M.byKey["core:" + w.core];
      if (o) o.workers.push(w); else M.unattr.push(w);
      if (w.line && w.node && !w.lingering) (M.byNode[w.line + "|" + w.node] = M.byNode[w.line + "|" + w.node] || []).push(w);
    });
    M.workers = workers;
    st._rm = { stale: li.stale, m: M };
    return M;
  }
  function descend(o, fn, seen) {
    seen = seen || {};
    o.kids.forEach(function (k) { if (seen[k.key]) return; seen[k.key] = 1; fn(k); descend(k, fn, seen); });
  }
  function treeWorkers(o) { var n = 0; descend(o, function (k) { n += k.workers.length; }); return n; }
  function subKids(o) { return o.kids.filter(function (k) { return k.type === "sub"; }); }
  function ancestry(M, o) {
    var out = [], g = 0;
    while (o && g++ < 64) { out.unshift(o); o = o.parentKey ? M.byKey[o.parentKey] : null; }
    return out;
  }
  function routerHref(pid, o) {
    return "#/p/" + encodeURIComponent(pid) + (o.type === "core" ? "/core/" + encodeURIComponent(o.id) :
      o.type === "line" ? "/line/" + encodeURIComponent(o.id) : "/r/" + encodeURIComponent(o.handle));
  }
  function crumbName(o) {
    if (o.type === "core") return coreShort(o.name);
    if (o.type === "line") return o.id;
    var s = splitName(o.name)[0];
    return s.length > 18 ? s.slice(0, 17) + "…" : s;
  }
  function routerPresence(o, li) {
    if (li.stale) return { p: null, out: null };
    var r = o.type === "line" ? (li.live.routers || {})[o.id] : null;
    var p = o.tn && o.tn.present != null ? o.tn.present : r ? r.present : null;
    var out = (o.tn && o.tn.last_output_at) || (r && r.last_output_at) || null;
    return { p: p, out: out };
  }
  function routerStatusHtml(o, li, who) {
    if (li.stale) return '<span class="rst unk">在跑数据' + (li.at ? "过期" : "未知") + "</span>";
    var s = routerPresence(o, li);
    return '<span class="rst ' + (s.p === true ? "on" : s.p === false ? "off" : "unk") + '" title="' + esc(s.out ? "最近输出 " + when(s.out) : "没有输出记录") + '">' +
      esc((who || "") + (s.p === true ? "在线" : s.p === false ? "不在线" : "在线情况未知") + (s.out ? " · 输出 " + rel(s.out) : "")) + "</span>";
  }

  // ---- counts summary (bar + bead-icon counts)
  var SUMS = [
    { st: ["reviewed", "delivered"], label: "完成", icon: "reviewed" },
    { st: ["running"], label: "在跑", icon: "running" },
    { st: ["ready"], label: "可派", icon: "ready" },
    { st: ["planned"], label: "已排", icon: "planned" },
    { st: ["blocked"], label: "卡住", icon: "blocked" },
    { st: ["failed"], label: "失败", icon: "failed" },
    { st: ["unknown"], label: "未知", icon: "unknown" },
    { st: ["superseded", "cancelled"], label: "取代/取消", icon: "superseded" }
  ];
  function statusCounts(ls) {
    var c = { _total: 0 };
    values(ls && ls.nodes).forEach(function (n) { var s = nodeStatus(n); c[s] = (c[s] || 0) + 1; c._total++; });
    return c;
  }
  function sumBar(c) {
    return '<div class="sbar" role="img" aria-label="' + esc(STATUS_ORDER.filter(function (s) { return c[s]; }).map(function (s) { return STATUS[s] + " " + c[s]; }).join("，")) + '">' +
      STATUS_ORDER.filter(function (s) { return c[s]; }).map(function (s) {
        return '<i class="seg sg-' + s + '" style="flex:' + c[s] + '" title="' + esc(STATUS[s] + " " + c[s]) + '"></i>';
      }).join("") + "</div>";
  }
  function sumChips(c) {
    var out = '<span class="tot">共 ' + c._total + "</span>";
    SUMS.forEach(function (g) {
      var n = 0;
      g.st.forEach(function (s) { n += c[s] || 0; });
      if (n) out += '<span class="sc" title="' + esc(g.label + " " + n) + '">' + beadIcon(g.icon, 14) + "<b>" + n + '</b><span class="sl">' + esc(g.label) + "</span></span>";
    });
    return out;
  }
  function doneTime(st, n) {
    var sp = nodeStamps(st, n);
    if (ms(sp.done) != null) return ms(sp.done);
    return n.imported ? ms(sp.disp) : ms(n.updated_at);
  }

  // ---- worker chips / cards
  function chipHtml(st, w, withNode) {
    var t = tierOf(w.model), e = wElapsed(w);
    var n = w.line && w.node ? findNode(st, w.line, w.node) : null;
    var lab = withNode && n ? n.title || n.id : "", sx = wStall(w);
    return '<span class="wchip' + (w.lingering ? " ling" : "") + (sx ? " stall " + sx.cls : "") + '" style="--tc:' + tierColor(t.id) + '" data-wkey="' + esc(wkey(w)) + '"' +
      (n ? ' data-line="' + esc(w.line) + '" data-node="' + esc(w.node) + '"' : "") + ' tabindex="0" role="button" aria-label="' +
      esc(modelShort(w.model) + " 执行者，" + (sx ? sx.long + "，" : "") + e.long + (n ? "，节点 " + (n.title || n.id) : "")) + '"' +
      (sx ? ' title="' + esc(sx.long + (sx.line ? "：" + sx.line : "")) + '"' : "") + '><i></i><b>' + esc(modelShort(w.model)) + '</b><span class="el">' + esc(sx ? sx.txt : e.txt) + "</span>" +
      (lab ? '<span class="nd">' + esc(lab) + "</span>" : "") + (isReview(n) ? REVIEW_TAG : "") + "</span>";
  }
  function chipsHtml(st, ws, max, withNode) {
    ws = sortWorkers(ws);
    var out = ws.slice(0, max).map(function (w) { return chipHtml(st, w, withNode); }).join("");
    if (ws.length > max) out += '<span class="more">+' + (ws.length - max) + "</span>";
    return out;
  }
  function wcardHtml(st, w) {
    var t = tierOf(w.model), e = wElapsed(w);
    var n = w.line && w.node ? findNode(st, w.line, w.node) : null;
    var what = n ? beadIcon(nodeStatus(n), 14) + "<span>" + esc(n.title || n.id) + "</span>" :
      "<span>" + esc(w.line ? w.line + " · 没对上具体节点" : w.worktree_name || "没对上节点") + "</span>";
    var sx = wStall(w), act = wActivity(w), rep = reportTail(n);
    return '<div class="wcard' + (w.lingering ? " ling" : "") + (sx ? " stall " + sx.cls : "") + '" style="--tc:' + tierColor(t.id) + '" data-wkey="' + esc(wkey(w)) + '"' +
      (n ? ' data-line="' + esc(w.line) + '" data-node="' + esc(w.node) + '"' : "") + ' tabindex="0" role="button" aria-label="' + esc(modelShort(w.model) + " 执行者，" + e.long) + '"' +
      (rep ? ' title="' + esc("报告：" + n.refs.report) + '"' : "") + '>' +
      '<div class="wc-h"><i></i><b>' + esc(modelShort(w.model)) + '</b><span class="el">' + esc(sx ? sx.txt : e.txt) + '</span><span class="host">' + esc(w.host || "未知机器") + "</span></div>" +
      '<div class="wc-n">' + what + "</div>" +
      '<div class="wc-s">' + esc((sx ? sx.long + (sx.line ? "：" + sx.line : "") : w.lingering ? e.long : "开始 " + abs(w.started_at) + (w.effort ? " · " + w.effort : "")) + (act ? " · " + act : "")) + "</div>" +
      (rep ? '<div class="wc-s" title="' + esc(n.refs.report) + '">' + esc("报告：" + rep) + "</div>" : "") + "</div>";
  }

  // ---- one line's compact summary: counts, running beads with their workers, a few recent results
  function litemHtml(st, lid, n, ws, small) {
    var s = nodeStatus(n), t = ws.length ? tierOf(ws[0].model) : null;
    return '<span class="litem' + (small ? " sm" : "") + (t ? " linked" : "") + '"' + (t ? ' style="--tc:' + tierColor(t.id) + '"' : "") + ">" +
      '<span class="bead lb" data-line="' + esc(lid) + '" data-node="' + esc(n.id) + '" tabindex="0" role="button" aria-label="' + esc((n.title || n.id) + "，" + STATUS[s]) + '">' +
      beadIcon(s, 16) + '<span class="lt">' + esc(n.title || n.id) + "</span>" + (isReview(n) ? REVIEW_TAG : "") + "</span>" +
      ws.map(function (w) { return chipHtml(st, w, false); }).join("") + "</span>";
  }
  function laneSummary(st, M, o, ls, li, maxRun) {
    var c = statusCounts(ls), lid = o.id;
    var kids = subKids(o), kidW = treeWorkers(o);
    var subind = kids.length ? '<span class="subind" title="' + esc(kids.map(function (k) { return k.name; }).join("、")) + '">' + icon("fork") +
      "子 router " + kids.length + (kidW ? " · " + kidW + " 执行者" : "") + "</span>" : "";
    var out = '<div class="lsum">' + (c._total ? sumBar(c) + '<div class="scount">' + sumChips(c) + "</div>" : '<span class="none">还没有登记节点</span>') + subind + "</div>";
    var nodes = values(ls.nodes);
    var wnode = {};
    o.workers.forEach(function (w) { if (w.line === lid && w.node && !w.lingering) wnode[w.node] = 1; });
    var run = nodes.filter(function (n) { return nodeStatus(n) === "running" || wnode[n.id]; })
      .sort(function (a, b) { return (nodeSortTime(st, b) || 0) - (nodeSortTime(st, a) || 0); });
    var shown = run.slice(0, maxRun), used = {};
    var ws = sortWorkers(o.workers);
    var items = shown.map(function (n) {
      var nws = ws.filter(function (w) { return w.line === lid && w.node === n.id; });
      nws.forEach(function (w) { used[wkey(w)] = 1; });
      return litemHtml(st, lid, n, nws);
    }).join("");
    if (run.length > shown.length) items += '<span class="more">另 ' + (run.length - shown.length) + " 个在跑</span>";
    var loose = ws.filter(function (w) { return !used[wkey(w)]; });
    items += chipsHtml(st, loose, maxRun + 2, true);
    if (items) out += '<div class="lrow"><span class="lk">在跑</span>' + items + "</div>";
    var done = nodes.filter(function (n) { var s = nodeStatus(n); return s === "delivered" || s === "reviewed" || s === "failed" || s === "blocked"; })
      .sort(function (a, b) { return (doneTime(st, b) || 0) - (doneTime(st, a) || 0); }).slice(0, maxRun > 4 ? 6 : 3);
    if (done.length) out += '<div class="lrow"><span class="lk">最近</span>' + done.map(function (n) { return litemHtml(st, lid, n, [], true); }).join("") + "</div>";
    return out;
  }
  function laneHtml(st, M, ln, li) {
    var o = M.byKey["line:" + ln.id], ls = st.state.lines[ln.id] || { nodes: {}, counts: {} };
    var lamp = lampOf(st, ln, ls, li), nm = splitName(ln.name, ln.id);
    var r = li.live && (li.live.routers || {})[ln.id];
    var sub = [];
    if (!li.stale && r && r.present != null) sub.push(r.present ? "在线" + (r.last_output_at ? " · 输出 " + rel(r.last_output_at) : "") : "终端不在线");
    if (r && r.screen_flag && SCREEN[r.screen_flag]) sub.push(SCREEN[r.screen_flag]);
    sub.push(ls.last_event_at ? "事件 " + rel(ls.last_event_at) : "还没有事件");
    var head = '<div class="lane-head" title="' + esc(lamp.why) + '">' +
      '<div class="avatar">' + avatarSvg(ln.id) + '<i class="lamp ' + lamp.c + '" aria-label="' + esc(lamp.why) + '"></i></div>' +
      '<div class="ln-txt"><div class="ln-name"><span class="idb">' + esc(ln.id) + "</span>" + esc(nm[0]) + (ls.open === false ? '<span class="closed">已关</span>' : "") + supBadge(supLine(supData(st.project.id), ln.id)) + "</div>" +
      (nm[1] ? '<div class="ln-desc">' + esc(nm[1]) + "</div>" : "") + '<div class="ln-sub">' + esc(sub.join(" · ")) + "</div></div></div>";
    return '<div class="lane' + (ls.open === false ? " is-closed" : "") + '" data-href="' + esc(routerHref(st.project.id, o)) + '" tabindex="0" role="link" aria-label="' + esc("进入 " + ln.id + " " + (ln.name || "")) + '">' +
      head + '<div class="lane-body">' + laneSummary(st, M, o, ls, li, 4) + '</div><span class="chev" aria-hidden="true">' + icon("chev") + "</span></div>";
  }
  function coreHeadHtml(st, M, core, li) {
    var o = M.byKey["core:" + core.id], cs = coreShort(core.name || core.id);
    var mine = o.workers.filter(function (w) { return w.router && sameH(w.router, o.handle); });
    var other = o.workers.filter(function (w) { return mine.indexOf(w) < 0; });
    var subs = subKids(o).length;
    var nl = (st.project.lines || []).filter(function (l) { return l.core === core.id; }).length;
    return '<div class="core-h" data-href="' + esc(routerHref(st.project.id, o)) + '" tabindex="0" role="link" aria-label="' + esc("进入 " + (core.name || core.id) + " 协调人视图") + '">' +
      '<span class="core-name">' + esc(core.name || core.id) + "</span>" + routerStatusHtml(o, li, "协调人") +
      (mine.length ? '<span class="core-ws"><span class="lk">由' + esc(cs) + "协调人派出</span>" + chipsHtml(st, mine, 6, true) + "</span>" : "") +
      (other.length ? '<span class="core-ws"><span class="lk">归到' + esc(cs) + "</span>" + chipsHtml(st, other, 6, true) + "</span>" : "") +
      '<span class="core-go">' + nl + " 条主线" + (subs ? " · 子 router " + subs : "") + icon("chev") + "</span></div>";
  }
  function hostsHtml(M, li) {
    if (li.stale) return "";
    var hosts = li.live.hosts || {}, n = {};
    M.workers.forEach(function (w) { var h = w.host || "未知机器"; n[h] = (n[h] || 0) + 1; });
    var names = Object.keys(hosts);
    Object.keys(n).forEach(function (h) { if (names.indexOf(h) < 0) names.push(h); });
    var ling = M.workers.filter(function (w) { return w.lingering; }).length;
    return names.map(function (h) {
      return hosts[h] === "unknown" ? '<span class="chip hostc unk" title="这台机器本次没采集">' + esc(h) + " 未知</span>" :
        '<span class="chip hostc">' + esc(h) + " " + (n[h] || 0) + "</span>";
    }).join("") + (ling ? '<span class="chip" title="任务已结束、进程还在，可能该清理">已结束仍在 ' + ling + "</span>" : "");
  }
  function lanesHtml(st, li) {
    var M = routerModel(st, li), doc = st.project, out = "";
    var known = {};
    (doc.cores || []).forEach(function (core) {
      known[core.id] = 1;
      var lines = (doc.lines || []).filter(function (l) { return l.core === core.id; });
      out += '<div class="core-sec">' + coreHeadHtml(st, M, core, li) + lines.map(function (ln) { return laneHtml(st, M, ln, li); }).join("") + "</div>";
    });
    var rest = (doc.lines || []).filter(function (l) { return !known[l.core]; });
    if (rest.length) out += '<div class="core-sec"><div class="core-h"><span class="core-name">其他</span></div>' + rest.map(function (ln) { return laneHtml(st, M, ln, li); }).join("") + "</div>";
    if (!out) return '<div class="empty">还没有声明主线</div>';
    var foot = "";
    if (M.unattr.length) foot += '<div class="lane-foot unattr"><span class="lk">未归属</span>' + chipsHtml(st, M.unattr, 12, false) + '<span class="hint">进程没对上任何主线或 router</span></div>';
    if (!li.stale && li.live.errors && li.live.errors.length) foot += '<div class="lane-foot"><span class="lk">采集提示</span><span class="hint">' + li.live.errors.map(esc).join("；") + "</span></div>";
    return out + foot;
  }
  function avatarSvg(id, dashed) {
    var h = hashStr(String(id)) % 360;
    var t = String(id).slice(0, 4);
    var fs = t.length > 3 ? 8.5 : 10;
    var gid = "av-" + (hashStr(String(id)) % 1e8);
    return '<svg viewBox="0 0 34 34" aria-hidden="true"><defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="1" y2="1">' +
      '<stop offset="0" stop-color="hsl(' + h + ' 45% 38%)"/><stop offset="1" stop-color="hsl(' + ((h + 40) % 360) + ' 45% 22%)"/></linearGradient></defs>' +
      '<path d="M17 1.5l13.4 7.75v15.5L17 32.5 3.6 24.75V9.25z" fill="url(#' + gid + ')" stroke="var(--line-2)" stroke-width="1"' + (dashed ? ' stroke-dasharray="3 2"' : "") + "/>" +
      '<text x="17" y="' + (17 + fs * 0.36) + '" text-anchor="middle" font-family="ui-monospace,Menlo,monospace" font-weight="700" font-size="' + fs + '" fill="#fff">' + esc(t) + "</text></svg>";
  }

  // lane layout: column = longest path over node.deps inside the line, same-depth nodes stacked (max LANE_ROWS)
  var LANE_ROWS = 3;
  function laneLayout(st, nodes) {
    var byId = {}, order = {};
    nodes.forEach(function (n, i) { byId[n.id] = n; order[n.id] = i; });
    var edges = [];
    nodes.forEach(function (n) { (n.deps || []).forEach(function (d) { if (byId[d] && d !== n.id) edges.push([d, n.id]); }); });
    function byTime(a, b) {
      var ta = nodeSortTime(st, a), tb = nodeSortTime(st, b);
      if (ta != null && tb != null && ta !== tb) return ta - tb;
      if ((ta == null) !== (tb == null)) return ta == null ? 1 : -1;
      return order[a.id] - order[b.id];
    }
    var pos = {};
    if (!edges.length) {
      nodes.slice().sort(byTime).forEach(function (n, i) { pos[n.id] = { col: i, row: 0 }; });
      return { pos: pos, edges: edges, cols: nodes.length, rows: 1, hasEdges: false };
    }
    var depth = {}, busy = {};
    function dep(n) {
      if (depth[n.id] != null) return depth[n.id];
      if (busy[n.id]) return 0;
      busy[n.id] = true;
      var d = 0;
      (n.deps || []).forEach(function (a) { if (byId[a] && a !== n.id) d = Math.max(d, dep(byId[a]) + 1); });
      busy[n.id] = false;
      return (depth[n.id] = d);
    }
    nodes.forEach(dep);
    var cols = {}, maxD = 0;
    nodes.forEach(function (n) { (cols[depth[n.id]] = cols[depth[n.id]] || []).push(n); maxD = Math.max(maxD, depth[n.id]); });
    var c = 0, rows = 1;
    for (var d = 0; d <= maxD; d++) {
      var col = (cols[d] || []).sort(byTime);
      for (var i = 0; i < col.length; i += LANE_ROWS) {
        col.slice(i, i + LANE_ROWS).forEach(function (n, r) { pos[n.id] = { col: c, row: r }; rows = Math.max(rows, r + 1); });
        c++;
      }
    }
    return { pos: pos, edges: edges, cols: c, rows: rows, hasEdges: true };
  }
  // all upstream and downstream node ids of one node (including itself)
  function lineClosure(ls, id) {
    var set = {}, kids = {};
    Object.keys(ls.nodes).forEach(function (k) { (ls.nodes[k].deps || []).forEach(function (d) { (kids[d] = kids[d] || []).push(k); }); });
    function up(i) { if (set[i] || !ls.nodes[i]) return; set[i] = true; (ls.nodes[i].deps || []).forEach(up); }
    var down = {};
    function dn(i) { if (down[i]) return; down[i] = true; set[i] = true; (kids[i] || []).forEach(dn); }
    up(id); dn(id);
    return set;
  }
  // ---- router page (#/p/<id>/core|line|r/<key>)
  function routerKey(sub) { return sub ? (sub.t === "r" ? "r:" : sub.t + ":") + sub.k : null; }
  function rcardHtml(st, M, k, li) {
    var nm = splitName(k.name, k.type === "line" ? k.id : null);
    var ls = k.type === "line" ? st.state.lines[k.id] || { nodes: {}, counts: {} } : null;
    var lamp = ls ? lampOf(st, k.decl, ls, li) : null;
    var c = ls ? statusCounts(ls) : null, subs = subKids(k), tw = treeWorkers(k);
    var kind = k.type === "line" ? "主线 router" : "子 router";
    return '<div class="rcard k-' + k.type + '" data-href="' + esc(routerHref(st.project.id, k)) + '" tabindex="0" role="link" aria-label="' + esc("进入 " + k.name) + '">' +
      '<div class="rc-h"><div class="avatar sm">' + avatarSvg(k.type === "line" ? k.id : nm[0], k.type === "sub") + (lamp ? '<i class="lamp ' + lamp.c + '"></i>' : "") + "</div>" +
      '<div class="rc-t"><b>' + (k.type === "line" ? '<span class="idb">' + esc(k.id) + "</span>" : "") + esc(nm[0]) + "</b><span>" + esc(kind + (nm[1] ? " · " + nm[1] : "")) + "</span></div>" +
      '<span class="chev">' + icon("chev") + "</span></div>" +
      (c && c._total ? sumBar(c) + '<div class="scount">' + sumChips(c) + "</div>" : '<div class="rc-note">' + (k.type === "sub" ? "还没有登记节点" : "还没有节点") + "</div>") +
      '<div class="rc-ws">' + (k.workers.length ? chipsHtml(st, k.workers, 8, true) : '<span class="none">' + (li.stale ? "在跑数据过期" : "没有自己派出的执行者") + "</span>") + "</div>" +
      '<div class="rc-f">' + routerStatusHtml(k, li) + (subs.length ? "<span>" + icon("fork") + "子 router " + subs.length + (tw ? " · " + tw + " 执行者" : "") + "</span>" : "") + "</div></div>";
  }
  var expanded = {};
  function gbeadHtml(st, ln, n, M) {
    var s = nodeStatus(n), ws = M.byNode[ln.id + "|" + n.id], t = ws && ws.length ? tierOf(ws[0].model) : null;
    return '<span class="bead gb' + (t ? " ringed" : "") + (n.imported ? " imp-b" : "") + '"' + (t ? ' style="--tc:' + tierColor(t.id) + '"' : "") +
      ' data-line="' + esc(ln.id) + '" data-node="' + esc(n.id) + '" tabindex="0" role="button" aria-label="' + esc((n.title || n.id) + "，" + STATUS[s]) + '">' + beadIcon(s, 24) + "</span>";
  }
  function dagHtml(st, M, ln, ls) {
    var nodes = values(ls.nodes);
    if (!nodes.length) return '<div class="empty">这条主线还没有节点</div>';
    var lay = laneLayout(st, nodes);
    if (!lay.hasEdges) {
      // no dependency info: wrap beads in time order, fold long runs of unknown/planned into one pill
      var sorted = nodes.slice().sort(function (a, b) { return lay.pos[a.id].col - lay.pos[b.id].col; });
      var key = st.project.id + "|" + ln.id, open = !!expanded[key], out = '<div class="bgrid">', i = 0, folded = false;
      while (i < sorted.length) {
        var s = nodeStatus(sorted[i]);
        if (s === "unknown" || s === "planned") {
          var j = i;
          while (j < sorted.length && nodeStatus(sorted[j]) === s) j++;
          if (j - i >= 6 && !open) {
            out += '<button class="pill fold" type="button" data-expand="' + esc(key) + '">' + beadIcon(s, 14) + "+" + (j - i) + " " + esc(STATUS[s]) + "</button>";
            i = j; folded = true; continue;
          }
          if (j - i >= 6) folded = true;
        }
        out += gbeadHtml(st, ln, sorted[i], M); i++;
      }
      if (open && folded) out += '<button class="pill fold" type="button" data-expand="' + esc(key) + '">收起</button>';
      return out + '</div><div class="lane-note">这条线的旧记录没有依赖信息，按时间排列</div>';
    }
    var step = 40, rowH = 42, r0 = 10, cy0 = 30, padX = 18;
    var width = padX * 2 + Math.max(1, lay.cols) * step, H = cy0 + (lay.rows - 1) * rowH + 24;
    function X(n) { return padX + step * (lay.pos[n.id].col + 0.5); }
    function Y(n) { return cy0 + lay.pos[n.id].row * rowH; }
    var svg = '<svg class="beads" width="' + width + '" height="' + H + '" viewBox="0 0 ' + width + " " + H + '" role="group" aria-label="' + esc(ln.name) + ' 的节点">';
    lay.edges.forEach(function (e) {
      var a = ls.nodes[e[0]], b = ls.nodes[e[1]];
      svg += '<path class="dep" data-a="' + esc(a.id) + '" data-b="' + esc(b.id) + '" d="' + edgePath(X(a) + r0, Y(a), X(b) - r0, Y(b)) + '"/>';
    });
    nodes.forEach(function (n) {
      if (n.status === "superseded" && n.by && lay.pos[n.by]) {
        var b = ls.nodes[n.by], x1 = X(n), x2 = X(b), ym = Math.min(Y(n), Y(b)) - 24;
        svg += '<path d="M' + x1 + " " + (Y(n) - 8) + " Q" + (x1 + x2) / 2 + " " + ym + " " + x2 + " " + (Y(b) - 12) + '" fill="none" stroke="var(--muted)" stroke-width="1.2" stroke-dasharray="2 3"/>';
      }
    });
    nodes.forEach(function (n) {
      var x = X(n), y = Y(n), s = nodeStatus(n), ws = M.byNode[ln.id + "|" + n.id];
      svg += '<g class="bead" data-line="' + esc(ln.id) + '" data-node="' + esc(n.id) + '" tabindex="0" role="button" aria-label="' + esc((n.title || n.id) + "，" + STATUS[s]) + '">' +
        '<rect class="hit" x="' + (x - step / 2) + '" y="' + (y - rowH / 2) + '" width="' + step + '" height="' + rowH + '" rx="9"/>' +
        (ws && ws.length ? '<circle class="wring" cx="' + x + '" cy="' + y + '" r="' + (r0 + 5) + '" style="stroke:' + tierColor(tierOf(ws[0].model).id) + '"/>' :
          n.imported ? '<circle class="imp" cx="' + x + '" cy="' + y + '" r="' + (r0 + 3.5) + '"/>' : "") +
        beadShape(s, x, y, r0) + "</g>";
    });
    return '<div class="dag-wrap" data-scroll-key="dag-' + esc(st.project.id + "-" + ln.id) + '">' + svg + "</svg></div>";
  }
  function routerPageHtml(st, key) {
    var li = liveInfo(st), M = routerModel(st, li), o = M.byKey[key], pid = st.project.id;
    var home = "#/p/" + encodeURIComponent(pid);
    if (!o) {
      return '<section class="panel"><div class="rv-bar"><a class="back" href="' + home + '">' + icon("back") + "返回项目</a></div><div class=\"empty\">" +
        (li.stale ? "在跑数据" + (li.at ? "过期" : "未知") + "，看不到这个 router 的位置。" : "找不到这个 router，它可能已经不在调度树里。") + "</div></section>";
    }
    var chain = ancestry(M, o), parent = chain.length > 1 ? chain[chain.length - 2] : null;
    var bc = '<a href="' + home + '">' + esc(coreShort(st.project.name) || pid) + "</a>" + chain.map(function (c, i) {
      return '<span class="sep">›</span>' + (i === chain.length - 1 ? "<b>" + esc(crumbName(c)) + "</b>" : '<a href="' + esc(routerHref(pid, c)) + '">' + esc(crumbName(c)) + "</a>");
    }).join("");
    var nm = splitName(o.name, o.type === "line" ? o.id : null);
    var ls = o.type === "line" ? st.state.lines[o.id] || { nodes: {}, counts: {} } : null;
    var lamp = ls ? lampOf(st, o.decl, ls, li) : null;
    var kindTxt = o.type === "core" ? "核心协调人" : o.type === "line" ? "主线 router · " + o.id + " · " + coreNameOf(st, o.core) :
      "子 router" + (o.line ? " · 属于 " + o.line : o.core ? " · " + coreNameOf(st, o.core) : "");
    var head = '<section class="panel rv-head"><div class="rv-bar"><a class="back" href="' + esc(parent ? routerHref(pid, parent) : home) + '">' + icon("back") + "返回</a>" +
      '<nav class="rv-crumbs" aria-label="位置">' + bc + '</nav><span class="sp"></span>' + (li.stale ? staleBadge(li) : timeTag(li.at, "快照 ")) + "</div>" +
      '<div class="rv-id"><div class="avatar lg">' + avatarSvg(o.type === "line" ? o.id : o.type === "core" ? o.id : nm[0], o.type === "sub") + (lamp ? '<i class="lamp ' + lamp.c + '"></i>' : "") + "</div>" +
      '<div class="rv-t"><div class="rv-kind">' + esc(kindTxt) + "</div><h1>" + esc(nm[0]) + "</h1>" + (nm[1] ? '<p class="rv-desc">' + esc(nm[1]) + "</p>" : "") +
      '<div class="meta">' + routerStatusHtml(o, li, o.type === "core" ? "协调人" : "") + (lamp ? '<span class="chip" title="' + esc(lamp.why) + '">' + esc(lamp.why) + "</span>" : "") +
      (ls && ls.last_event_at ? timeTag(ls.last_event_at, "事件 ") : "") +
      (o.type === "line" ? '<button class="pill" type="button" data-line-detail="' + esc(o.id) + '">节点列表与详情</button>' : "") + "</div></div></div>" +
      (ls ? '<div class="rv-sum">' + laneSummary(st, M, o, ls, li, 8) + "</div>" : "") + "</section>";
    var tw = treeWorkers(o);
    var ws = sortWorkers(o.workers);
    var wp = '<section class="panel"><div class="panel-h"><h2>' + (o.type === "core" ? "协调人派出的执行者" : "它派出的执行者") + '</h2><span class="chip">' + ws.length + " 个</span>" +
      (tw ? '<span class="chip">下级 router 另有 ' + tw + " 个</span>" : "") + '<span class="sp"></span><span class="chip">悬停卡片，对应的节点会亮起</span></div>' +
      (ws.length ? '<div class="wcards">' + ws.map(function (w) { return wcardHtml(st, w); }).join("") + "</div>" :
        '<div class="empty sm">' + (li.stale ? "在跑数据" + (li.at ? "过期" : "未知") + "，不显示旧数据" : "现在没有在跑的执行者") + "</div>") + "</section>";
    var kids = o.kids.slice();
    if (o.type === "core") {
      var declared = (st.project.lines || []).filter(function (l) { return l.core === o.id; }).map(function (l) { return M.byKey["line:" + l.id]; });
      kids = declared.concat(kids.filter(function (k) { return declared.indexOf(k) < 0; }));
    }
    var nLine = kids.filter(function (k) { return k.type === "line"; }).length, nSub = kids.length - nLine;
    var kp = kids.length ? '<section class="panel"><div class="panel-h"><h2>' + (o.type === "core" ? "下属 router" : "子 router") + "</h2>" +
      (nLine ? '<span class="chip">主线 ' + nLine + "</span>" : "") + (nSub ? '<span class="chip">子 router ' + nSub + "</span>" : "") +
      '<span class="sp"></span><span class="chip">点卡片进入</span></div><div class="rcards">' + kids.map(function (k) { return rcardHtml(st, M, k, li); }).join("") + "</div></section>" : "";
    var body;
    if (ls) {
      var c = statusCounts(ls);
      body = '<section class="panel"><div class="panel-h"><h2>节点图</h2><span class="chip">' + c._total + " 个节点</span>" +
        '<span class="chip">彩色外圈 = 有执行者在跑，颜色是模型档位</span></div>' + dagHtml(st, M, o.decl, ls) + legendNodes() + legendTiers("") + "</section>";
    } else if (o.type === "sub") {
      body = '<section class="panel"><div class="empty">这个 router 还没有登记节点，只显示它派出的执行者。</div></section>';
    } else body = "";
    var order = o.type === "core" ? [head, wp, kp] : [head, body, wp, kp];
    var lineSet = {};
    if (o.type === "core") (st.project.lines || []).forEach(function (l) { if (l.core === o.id) lineSet[l.id] = 1; });
    else if (o.line) lineSet[o.line] = 1;
    var feed = '<section class="panel" aria-label="实时动态"><div class="panel-h"><h2>实时动态</h2><span class="chip">只看' + esc(o.type === "core" ? coreShort(o.name) : o.line || "这个 router") + "</span></div>" +
      feedHtml(st, 60, function (e) { return o.type === "sub" && !o.line ? false : !!lineSet[e.line]; }) + "</section>";
    return '<div class="layout rv"><div class="col">' + order.join("") + '</div><div class="feed-col">' + feed + "</div></div>";
  }

  // ---- feed
  var ORIG_T = /(\d{4}-)?\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}/;
  var openGroups = {};
  function hmTime(iso) {
    var t = ms(iso);
    if (t == null) return "未知";
    var d = new Date(t), n = new Date();
    function p(x) { return (x < 10 ? "0" : "") + x; }
    var hm = p(d.getHours()) + ":" + p(d.getMinutes());
    return d.toDateString() === n.toDateString() ? hm : p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + hm;
  }
  // consecutive import (or patrol) events written within a few minutes of each other fold into one row
  var BURST_GAP = 5 * 60 * 1000;
  function feedGroups(feed) {
    var out = [], i = 0;
    while (i < feed.length) {
      var role = (feed[i].actor || {}).role;
      if (role === "import" || role === "patrol") {
        var j = i + 1;
        while (j < feed.length && (feed[j].actor || {}).role === role && Math.abs((ms(feed[j - 1].at) || 0) - (ms(feed[j].at) || 0)) <= BURST_GAP) j++;
        if (j - i >= (role === "import" ? 2 : 4)) { out.push({ role: role, list: feed.slice(i, j) }); i = j; continue; }
      }
      out.push({ ev: feed[i] }); i++;
    }
    return out;
  }
  function feedRow(e) {
    var k = KIND[e.kind] || [e.kind || "事件", "scan"];
    var role = (e.actor && e.actor.role) || "";
    var cls = e.kind === "nudge" ? "nudge" : e.kind === "auto_dispatch" ? "auto" : e.kind === "observe" ? "observe" : "";
    if (e.id && !(e.id in seenFeed)) seenFeed[e.id] = cache._feedPrimed ? false : true;
    var clickable = (e.line && e.node) || e.milestone;
    var orig = ORIG_T.test(e.summary || "");
    return '<li class="fi ' + cls + (clickable ? " clickable" : "") + '" data-feed-id="' + esc(e.id || "") + '"' +
      (e.node ? ' data-line="' + esc(e.line) + '" data-node="' + esc(e.node) + '"' : e.milestone ? ' data-ms="' + esc(e.milestone) + '"' : "") + ">" +
      '<span class="ic" style="color:' + (KIND_COLOR[e.kind] || "var(--ink-2)") + '">' + icon(k[1]) + "</span>" +
      '<div class="fb"><div class="r1">' + (role ? '<span class="role r-' + esc(role) + '">' + esc(ROLE[role] || role) + "</span>" : "") +
      '<b class="kn">' + esc(k[0]) + "</b>" + (e.line ? '<span class="ln">' + esc(e.line) + "</span>" : e.milestone ? '<span class="ln">' + esc(e.milestone) + "</span>" : "") +
      (e.outcome ? '<span class="oc o-' + esc(e.outcome) + '">' + esc(OUTCOME[e.outcome] || e.outcome) + "</span>" : "") +
      '<span class="t" title="' + esc((orig ? "写入看板的时间，原始时间见正文：" : "") + when(e.at)) + '">' + (orig ? "记录于 " : "") + esc(hmTime(e.at)) + "</span></div>" +
      (e.node ? '<div class="nd" title="' + esc(e.node) + '">' + esc(e.node) + "</div>" : "") +
      '<div class="txt">' + (e.kind === "decision" && DECISION[e.result] ? "[" + esc(DECISION[e.result]) + "] " : "") + esc(e.summary || "") + "</div></div></li>";
  }
  function feedHtml(st, limit, keep) {
    var feed = (st.state.feed || []).filter(function (e) { return !keep || keep(e); }).slice(0, limit);
    if (!feed.length) return '<div class="empty">还没有事件</div>';
    var pid = st.project.id;
    return '<ul class="feed">' + feedGroups(feed).map(function (g) {
      if (g.ev) return feedRow(g.ev);
      var gid = pid + "|" + (g.list[0].id || g.list[0].at);
      var by = {}, order = [];
      g.list.forEach(function (e) { var l = e.line || e.milestone || "其他"; if (!by[l]) { by[l] = 0; order.push(l); } by[l]++; });
      order.sort(function (a, b) { return by[b] - by[a]; });
      var corr = g.list.every(function (e) { return /^更正/.test(e.summary || ""); });
      var title = g.role === "import" ? (corr ? "导入更正" : "导入记录") : "巡检";
      var lines = order.slice(0, 4).map(function (l) { return l + " " + by[l]; }).join("、") + (order.length > 4 ? "…" : "");
      return '<li class="fgroup r-' + g.role + '"><details data-gid="' + esc(gid) + '"' + (openGroups[gid] ? " open" : "") + "><summary>" +
        '<span class="ic">' + icon(g.role === "import" ? "layers" : "scan") + "</span>" +
        '<div class="fb"><div class="r1"><span class="role r-' + g.role + '">' + esc(ROLE[g.role]) + '</span><b class="kn">' + esc(title) + " " + g.list.length + " 条</b>" +
        '<span class="t" title="' + esc("集中写入于 " + when(g.list[g.list.length - 1].at) + " 到 " + when(g.list[0].at)) + '">记录于 ' + esc(hmTime(g.list[0].at)) + "</span></div>" +
        '<div class="txt">' + esc(lines) + '<span class="gx">' + icon("chev") + "</span></div></div></summary>" +
        '<ul class="feed sub">' + g.list.map(feedRow).join("") + "</ul></details></li>";
    }).join("") + "</ul>";
  }

  // ----------------------------------------------------------- lookups
  function stateFor(el) {
    var col = el.closest && el.closest("[data-pid]");
    var id = col ? col.getAttribute("data-pid") : view.ids[0];
    return cache.states[id];
  }
  function findNode(st, lid, nid) { var ls = st && st.state.lines[lid]; return ls && ls.nodes[nid]; }

  // ----------------------------------------------------------- tooltip + links
  function showTip(html, x, y) {
    tip.innerHTML = html; tip.hidden = false;
    var w = tip.offsetWidth, h = tip.offsetHeight;
    var left = Math.min(window.innerWidth - w - 8, x + 14), top = y + 16;
    if (top + h > window.innerHeight - 8) top = y - h - 12;
    tip.style.left = Math.max(8, left) + "px"; tip.style.top = Math.max(8, top) + "px";
  }
  function hideTip() { tip.hidden = true; }
  function clearLinks() {
    links.innerHTML = "";
    document.querySelectorAll(".hl").forEach(function (e) { e.classList.remove("hl"); });
    document.querySelectorAll(".dim").forEach(function (e) { e.classList.remove("dim"); });
  }
  function center(el) { var r = el.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; }
  function drawLink(a, b) {
    var p = center(a), q = center(b);
    var my = Math.min(p.y, q.y) - 30;
    links.insertAdjacentHTML("beforeend", '<path d="M' + p.x + " " + p.y + " C" + p.x + " " + my + " " + q.x + " " + my + " " + q.x + " " + q.y + '"/><circle cx="' + q.x + '" cy="' + q.y + '" r="3"/>');
  }
  function scopeOf(el) { return el.closest("[data-pid]") || app; }
  function hoverBead(g, ev) {
    var st = stateFor(g), n = findNode(st, g.getAttribute("data-line"), g.getAttribute("data-node"));
    if (!n) return;
    clearLinks();
    g.classList.add("hl");
    var scope = scopeOf(g);
    (n.milestones || []).forEach(function (mid) {
      var s = scope.querySelector('.station[data-ms="' + cssEsc(mid) + '"]');
      if (s) { s.classList.add("hl"); drawLink(g, s); }
    });
    // dim everything in this lane except the node's upstream/downstream chain
    pairHl(scope, g.getAttribute("data-line"), g.getAttribute("data-node"));
    var lane = g.closest(".dag-wrap"), ls = st.state.lines[g.getAttribute("data-line")];
    if (lane && ls) {
      var chain = lineClosure(ls, n.id);
      lane.classList.add("focus");
      lane.querySelectorAll(".bead").forEach(function (b) { if (!chain[b.getAttribute("data-node")]) b.classList.add("dim"); });
      lane.querySelectorAll(".dep").forEach(function (e) {
        if (chain[e.getAttribute("data-a")] && chain[e.getAttribute("data-b")]) e.classList.add("hl"); else e.classList.add("dim");
      });
    }
    var s0 = nodeStatus(n);
    var extra = [];
    if (n.outcome) extra.push("交付：" + (OUTCOME[n.outcome] || n.outcome));
    if (n.review) extra.push("审查：" + (OUTCOME[n.review] || n.review));
    if (n.blocker) extra.push(BLOCKER[n.blocker] || n.blocker);
    if (n.milestones && n.milestones.length) extra.push("服务 " + n.milestones.join("、"));
    showTip('<div class="h">' + beadIcon(s0, 16) + esc(n.title || n.id) + '</div><div class="s">' + esc(STATUS[s0]) + (extra.length ? " · " + esc(extra.join(" · ")) : "") +
      '</div><div class="s">' + esc(n.summary || "") + '</div><div class="s" style="color:var(--muted)">' + esc(nodeTimeText(st, n)) + " · 点击看详情</div>", ev.clientX, ev.clientY);
  }
  function hoverStation(g, ev) {
    var st = stateFor(g);
    clearLinks();
    g.classList.add("hl");
    if (g.hasAttribute("data-goal")) {
      var fg = (st && st.project.final_goal) || {};
      showTip('<div class="h">最终目标</div><div class="s">' + esc(fg.text || "未写") + "</div>" + (fg.done_when ? '<div class="s" style="color:var(--muted)">达成条件：' + dwHtml(fg.done_when) + "</div>" : ""), ev.clientX, ev.clientY);
      return;
    }
    var mid = g.getAttribute("data-ms");
    var m = msMerged(st).filter(function (x) { return x.id === mid; })[0];
    if (!m) return;
    var scope = scopeOf(g), count = 0;
    scope.querySelectorAll(".bead").forEach(function (b) {
      var n = findNode(st, b.getAttribute("data-line"), b.getAttribute("data-node"));
      if (n && (n.milestones || []).indexOf(mid) >= 0) {
        b.classList.add("hl"); count++;
        var r = b.getBoundingClientRect();
        if (r.bottom > 0 && r.top < window.innerHeight) drawLink(g, b);
      }
    });
    showTip('<div class="h">' + stationIcon(m.status, 16) + esc(m.id + " " + (m.title || "")) + '</div><div class="s">' + esc(MS_STATUS[m.status]) + " · " + count + " 个节点服务它</div>" +
      (m.done_when ? '<div class="s" style="color:var(--muted)">达成条件：' + esc(m.done_when) + "</div>" : ""), ev.clientX, ev.clientY);
  }
  function workerOf(g) {
    var st = stateFor(g), li = liveInfo(st);
    if (!li.live) return null;
    var key = g.getAttribute("data-wkey");
    return (li.live.workers || []).filter(function (w) { return String(w.workspace || w.pid) === key; })[0] || null;
  }
  // worker chip/card <-> its running bead: both get the .hl ring in the worker's tier colour
  function pairHl(scope, lid, nid) {
    if (!lid || !nid) return;
    var sel = '[data-line="' + cssEsc(lid) + '"][data-node="' + cssEsc(nid) + '"]';
    scope.querySelectorAll(".bead" + sel + ", .wchip" + sel + ", .wcard" + sel).forEach(function (e) { e.classList.add("hl"); });
  }
  function hoverWorker(g, ev) {
    var w = workerOf(g);
    if (!w) return;
    clearLinks();
    g.classList.add("hl");
    var st = stateFor(g), scope = scopeOf(g);
    pairHl(scope, g.getAttribute("data-line"), g.getAttribute("data-node"));
    var li = liveInfo(st), t = tierOf(w.model), e = wElapsed(w);
    var n = w.line && w.node ? findNode(st, w.line, w.node) : null;
    showTip('<div class="h"><i class="wdot" style="color:' + tierColor(t.id) + '"></i>' + esc(modelShort(w.model) + " · " + t.name + " 档") + "</div>" +
      '<div class="s">' + esc(n ? w.line + " / " + (n.title || n.id) : w.line ? w.line + " · 没对上具体节点" : w.router ? "不属于任何主线" : "未归属") + "</div>" +
      (w.router ? '<div class="s">' + esc(sender(li.live, w)) + "</div>" + '<div class="s" style="color:var(--muted)">' + esc("调度链：" + chainText(li.live, w.router)) + "</div>" : "") +
      '<div class="s">' + esc((w.host || "未知机器") + " · " + e.long) + "</div>" +
      (wStall(w) ? '<div class="s" style="color:var(--st-' + (wStall(w).cls === "bad" ? "failed" : "blocked") + ')">' + esc(wStall(w).long + (wStall(w).line ? "：" + wStall(w).line : "")) + "</div>" : "") +
      (n && isReview(n) ? '<div class="s">审查节点</div>' : "") +
      (w.task ? '<div class="s">' + esc("任务 " + (TASK_STATE[w.task.state] || w.task.state || "状态未知")) + "</div>" : "") +
      (w.attribution ? '<div class="s" style="color:var(--muted)">' + esc("归属方式：" + (ATTRIB[w.attribution] || w.attribution)) + "</div>" : "") +
      '<div class="s" style="color:var(--muted)">' + esc("开始 " + when(w.started_at) + " · 点击看详情") + "</div>", ev.clientX, ev.clientY);
  }

  // touch taps synthesise mouseover at the tap point; after a navigation that lands on new content, so skip hover for touch
  var lastPointer = "mouse";
  document.addEventListener("pointerdown", function (ev) { lastPointer = ev.pointerType || "mouse"; }, true);
  document.addEventListener("pointermove", function (ev) { if (ev.pointerType === "mouse") lastPointer = "mouse"; }, true);
  document.addEventListener("mouseover", function (ev) {
    var t = ev.target;
    if (!t.closest || lastPointer === "touch") return;
    var g;
    if ((g = t.closest(".bead"))) return hoverBead(g, ev);
    if ((g = t.closest(".track-svg .station"))) return hoverStation(g, ev);
    if ((g = t.closest(".wchip, .wcard"))) return hoverWorker(g, ev);
  });
  document.addEventListener("mousemove", function (ev) {
    if (tip.hidden) return;
    var t = ev.target;
    if (t.closest && (t.closest(".bead") || t.closest(".track-svg .station") || t.closest(".wchip, .wcard"))) showTip(tip.innerHTML, ev.clientX, ev.clientY);
  });
  document.addEventListener("mouseout", function (ev) {
    var t = ev.target, to = ev.relatedTarget;
    if (!t.closest) return;
    var g = t.closest(".bead, .track-svg .station, .wchip, .wcard");
    if (g && !(to && g.contains(to))) { hideTip(); clearLinks(); }
  });
  window.addEventListener("scroll", function () { hideTip(); clearLinks(); }, true);

  // ----------------------------------------------------------- drawer
  function openDrawer(html) {
    hideTip(); clearLinks();
    drawerBody.innerHTML = html;
    drawer.classList.add("open"); drawer.setAttribute("aria-hidden", "false"); scrim.hidden = false;
    drawer.focus();
  }
  function closeDrawer() { drawer.classList.remove("open"); drawer.setAttribute("aria-hidden", "true"); scrim.hidden = true; }
  function dl(rows) {
    return "<dl>" + rows.filter(function (r) { return r; }).map(function (r) { return "<dt>" + esc(r[0]) + "</dt><dd>" + (r[2] ? r[1] : esc(r[1] == null || r[1] === "" ? "未知" : r[1])) + "</dd>"; }).join("") + "</dl>";
  }
  function codeCopy(s) { return s ? "<code>" + esc(s) + '</code><button class="copy" type="button" data-copy="' + esc(s) + '">复制</button>' : "未知"; }
  function nodeDrawer(st, lid, nid) {
    var n = findNode(st, lid, nid);
    if (!n) return;
    var ln = (st.project.lines || []).filter(function (l) { return l.id === lid; })[0] || {};
    var s = nodeStatus(n), refs = n.refs || {};
    var sp = nodeStamps(st, n);
    var downstream = Object.keys((st.state.lines[lid] || { nodes: {} }).nodes).filter(function (k) { return (st.state.lines[lid].nodes[k].deps || []).indexOf(n.id) >= 0; });
    function pills(l, ids) {
      var ns = st.state.lines[l].nodes;
      return (ids || []).length ? ids.map(function (d) {
        var o = ns[d];
        return '<button class="pill" type="button" data-open-node="' + esc(l + "|" + d) + '" title="' + esc(o ? o.title || d : "不在这条线里") + '">' + esc(d) + "</button>";
      }).join("") : "无";
    }
    var li = liveInfo(st), w = null;
    if (li.live) w = (li.live.workers || []).filter(function (x) { return x.line === lid && x.node === nid; })[0];
    var html = "<h3>" + esc(n.title || n.id) + '</h3><div class="kick">' + esc((ln.name || lid) + " · " + n.id) + (n.imported ? " · 从旧记录导入" : "") + (isReview(n) ? " · 审查节点" : "") + "</div>";
    html += '<div class="stline">' + beadIcon(s, 22) + esc(STATUS[s]) + (n.blocker ? " · " + esc(BLOCKER[n.blocker] || n.blocker) : "") + "</div>";
    html += '<div class="sum">' + esc(n.summary || "没有说明") + "</div>";
    html += dl([
      n.kind ? ["节点类型", n.kind === "review" ? "审查（只读和判断，不重跑重活）" : "执行（开发/跑任务）"] : null,
      w && wStall(w) ? ["worker 屏幕", wStall(w).long + (wStall(w).line ? "：" + wStall(w).line : "")] : null,
      ["交付结果", n.outcome ? OUTCOME[n.outcome] || n.outcome : "还没交付"],
      ["审查结论", n.review ? OUTCOME[n.review] || n.review : "还没审查"],
      ["模型", n.model || (n.route && n.route.model)],
      w && wActivity(w) ? ["worker 输出", wActivity(w)] : null,
      w ? ["正在跑", (w.host || "未知机器") + " · 已跑 " + dur((Date.now() - (ms(w.started_at) || Date.now())) / 1000)] : null,
      ["报告", codeCopy(refs.report), true],
      refs.head ? ["代码版本", "<code>" + esc(refs.head) + "</code>", true] : null,
      n.spec ? ["规格", codeCopy(n.spec), true] : null,
      ["服务关键节点", (n.milestones || []).length ? n.milestones.map(function (m) { return '<button class="pill" type="button" data-open-ms="' + esc(m) + '">' + esc(m) + "</button>"; }).join("") : "无", true],
      ["前置", pills(lid, n.deps), true],
      ["后续", pills(lid, downstream), true],
      n.by ? ["被谁取代", '<button class="pill" type="button" data-open-node="' + esc(lid + "|" + n.by) + '">' + esc(n.by) + "</button>", true] : null,
      (n.supersedes || []).length ? ["取代了", n.supersedes.map(function (d) { return '<button class="pill" type="button" data-open-node="' + esc(lid + "|" + d) + '">' + esc(d) + "</button>"; }).join(""), true] : null,
      refs.task ? ["任务", sp.task ? refs.task + " · " + (TASK_STATE[sp.task.state] || sp.task.state || "状态未知") : refs.task] : null
    ].concat(n.imported ? [
      sp.none ? ["时间", NO_TIME] : null,
      sp.disp ? ["派出", when(sp.disp)] : null,
      sp.done ? ["结束", when(sp.done)] : null
    ] : [
      ["排好", when(n.planned_at)],
      n.dispatched_at ? ["派出", when(n.dispatched_at) + (n.auto ? "（巡检代派）" : "")] : null,
      n.delivered_at ? ["交付", when(n.delivered_at)] : null,
      n.reviewed_at ? ["审查", when(n.reviewed_at)] : null
    ]).concat([
      n.expected_duration_minutes ? ["预计用时", n.expected_duration_minutes + " 分钟"] : null
    ]));
    html += "<h4>状态历史</h4><ol class=\"hist\">" + (n.history || []).map(function (h) {
      var k = KIND[h.kind] || [h.kind];
      return '<li><span class="k">' + esc(k[0]) + (h.outcome ? " · " + esc(OUTCOME[h.outcome] || h.outcome) : "") + '</span><span class="t">' + esc(h.source === "import" ? "导入记录" : when(h.at)) + "</span>" +
        (h.summary ? '<div class="s">' + esc(h.summary) + (h.actor ? " · " + esc(ROLE[h.actor] || h.actor) : "") + "</div>" : "") + "</li>";
    }).join("") + "</ol>";
    openDrawer(html);
  }
  function msDrawer(st, mid) {
    var m = msMerged(st).filter(function (x) { return x.id === mid; })[0];
    if (!m) return;
    var f = m.fold || {};
    var core = (st.project.cores || []).filter(function (c) { return c.id === m.core; })[0] || {};
    var serving = [];
    Object.keys(st.state.lines).forEach(function (lid) {
      var ns = st.state.lines[lid].nodes;
      Object.keys(ns).forEach(function (k) { if ((ns[k].milestones || []).indexOf(mid) >= 0) serving.push([lid, ns[k]]); });
    });
    var html = "<h3>" + esc(m.title || m.id) + '</h3><div class="kick">关键节点 ' + esc(m.id) + " · " + esc(core.name || m.core || "") + "</div>";
    html += '<div class="stline">' + stationIcon(m.status, 22) + esc(MS_STATUS[m.status]) + "</div>";
    if (m.done_when) html += '<div class="sum">达成条件：' + esc(m.done_when) + "</div>";
    var last = (f.claims || [])[f.claims ? f.claims.length - 1 : 0];
    html += dl([
      ["负责主线", (m.lines || []).join("、") || "无"],
      ["前置", (m.after || []).length ? m.after.map(function (a) { return '<button class="pill" type="button" data-open-ms="' + esc(a) + '">' + esc(a) + "</button>"; }).join("") : "无", true],
      ["报达成", f.claimed_at ? when(f.claimed_at) : "还没有"],
      last && last.refs && last.refs.report ? ["证据", codeCopy(last.refs.report), true] : null,
      f.confirmed_at ? ["确认", when(f.confirmed_at) + (f.confirm && f.confirm.summary ? " · " + f.confirm.summary : "")] : null,
      f.rejected ? ["上次驳回", when(f.rejected.at) + " · " + (f.rejected.summary || "")] : null
    ]);
    if ((f.claims || []).length) {
      html += '<h4>报告记录</h4><ol class="hist">' + f.claims.map(function (c) {
        return '<li><span class="k">' + esc(c.line || "") + '</span><span class="t">' + esc(when(c.at)) + '</span><div class="s">' + esc(c.summary || "") + "</div></li>";
      }).join("") + "</ol>";
    }
    html += "<h4>服务它的节点（" + serving.length + "）</h4><div>" + (serving.length ? serving.map(function (p) {
      var s = nodeStatus(p[1]);
      return '<button class="pill" type="button" data-open-node="' + esc(p[0] + "|" + p[1].id) + '" style="display:inline-flex;align-items:center;gap:4px">' + beadIcon(s, 14) + esc(p[1].title || p[1].id) + "</button>";
    }).join("") : '<span style="color:var(--muted)">还没有节点声明服务它</span>') + "</div>";
    openDrawer(html);
  }
  function goalDrawer(st) {
    var fg = st.project.final_goal || {}, S = st.state;
    var html = '<h3>最终目标</h3><div class="kick">' + esc(st.project.name) + '</div><div class="sum">' + esc(fg.text || "未写") + "</div>";
    html += dl([["达成条件", dwHtml(fg.done_when), true], ["关键节点", S.progress.confirmed + " / " + S.progress.total + " 已确认，" + S.progress.claimed + " 个等确认"]]);
    html += "<h4>关键节点</h4><div>" + msMerged(st).map(function (m) {
      return '<button class="pill" type="button" data-open-ms="' + esc(m.id) + '" style="display:inline-flex;align-items:center;gap:4px">' + stationIcon(m.status, 14) + esc(m.id + " " + (m.title || "")) + "</button>";
    }).join("") + "</div>";
    openDrawer(html);
  }
  function lineDrawer(st, lid) {
    var ln = (st.project.lines || []).filter(function (l) { return l.id === lid; })[0];
    if (!ln) return;
    var ls = st.state.lines[lid] || { nodes: {}, counts: {} };
    var li = liveInfo(st), lamp = lampOf(st, ln, ls, li);
    var r = li.live && (li.live.routers || {})[lid];
    var core = (st.project.cores || []).filter(function (c) { return c.id === ln.core; })[0] || {};
    var html = "<h3>" + esc(ln.name || lid) + '</h3><div class="kick">主线 ' + esc(lid) + " · " + esc(core.name || "") + "</div>";
    var lampName = { green: "正常", yellow: "安静", red: "要处理", grey: "看不出" }[lamp.c];
    html += '<div class="stline"><i class="lamp ' + lamp.c + '" style="position:static;display:inline-block;width:12px;height:12px;border-radius:50%"></i>' + esc(lampName + "：" + lamp.why) + "</div>";
    html += dl([
      ["负责人终端", li.stale ? "在跑数据过期，未知" : r ? (r.present ? "在线" : "不在线") + (r.last_output_at ? "，最近输出 " + rel(r.last_output_at) : "") : "快照里没有"],
      r && r.screen_flag ? ["末屏", SCREEN[r.screen_flag] || r.screen_flag] : null,
      ["最近事件", ls.last_event_at ? when(ls.last_event_at) : "还没有"],
      ["巡检", ln.patrol ? "开" : "关"],
      ["返工", (ls.rework || 0) + " 个节点被取代"],
      (ls.nudges || []).length ? ["被催", ls.nudges.length + " 次，最近 " + rel(ls.nudges[ls.nudges.length - 1])] : null
    ]);
    var nodes = Object.keys(ls.nodes).map(function (k) { return ls.nodes[k]; }).sort(function (a, b) { return (nodeSortTime(st, a) || 0) - (nodeSortTime(st, b) || 0); });
    html += "<h4>节点列表（" + nodes.length + "）</h4><ol class=\"hist plain\">" + nodes.map(function (n) {
      var s = nodeStatus(n);
      return '<li data-open-node="' + esc(lid + "|" + n.id) + '">' + beadIcon(s, 18) +
        '<div style="min-width:0"><span class="k">' + esc(n.title || n.id) + '</span><span class="t">' + esc(STATUS[s] + " · " + (n.imported ? nodeTimeText(st, n) : when(n.updated_at))) + '</span><div class="s">' + esc(n.summary || "") + "</div></div></li>";
    }).join("") + "</ol>";
    openDrawer(html);
  }
  function workerDrawer(g) {
    var w = workerOf(g);
    if (!w) return;
    var t = tierOf(w.model), run = ms(w.started_at) == null ? null : (Date.now() - ms(w.started_at)) / 1000;
    var li2 = liveInfo(stateFor(g)), lv = li2.live;
    var direct = !w.line && w.router;
    var html = '<h3>' + esc(w.line ? (w.node || "没对上节点") : direct ? routerName(lv, w.router) + " 派出的执行者" : "未归属的执行者") + '</h3><div class="kick">' + esc(w.line ? "主线 " + w.line : direct ? "不属于任何主线" : "进程没对上任何派出记录") + (w.lingering ? " · 任务已结束，进程还在" : "") + "</div>";
    html += '<div class="stline"><i class="wdot" style="color:' + tierColor(t.id) + '"></i>' + esc(t.name) + " 档</div>";
    var wsx = wStall(w);
    html += dl([wsx ? ["屏幕状态", wsx.long + (w.screen.checked_at ? "（" + when(w.screen.checked_at) + " 查看）" : "")] : null, wsx && wsx.line ? ["屏幕内容", wsx.line] : null,
      ["机器", w.host || "未知"], ["模型", w.model], ["强度", w.effort], ["开始", when(w.started_at)], ["已跑", dur(run)],
      wActivity(w) ? ["输出情况", wActivity(w) + (w.title ? "（" + w.title + "）" : "")] : null,
      w.task ? ["任务", w.task.id + " · " + (TASK_STATE[w.task.state] || w.task.state || "状态未知")] : null,
      w.task && w.task.run ? ["orca run", w.task.run] : null,
      w.task && w.task.dispatched_at ? ["任务派出", when(w.task.dispatched_at)] : null,
      w.task && w.task.completed_at ? ["任务结束", when(w.task.completed_at)] : null,
      w.attribution ? ["归属方式", ATTRIB[w.attribution] || w.attribution] : null,
      w.router ? ["派出者", routerLabel(lv, w.router)] : null,
      w.router ? ["调度链", chainText(lv, w.router)] : null,
      ["工作区", w.worktree_name], ["进程号", w.pid != null ? String(w.pid) : null]]);
    if (w.line && w.node) html += '<button class="pill" type="button" data-open-node="' + esc(w.line + "|" + w.node) + '">看这个节点</button>';
    openDrawer(html);
  }

  // ----------------------------------------------------------- clicks
  function activate(t) {
    var g, st;
    if ((g = t.closest("[data-copy]"))) {
      var txt = g.getAttribute("data-copy");
      if (navigator.clipboard) navigator.clipboard.writeText(txt).then(function () { g.textContent = "已复制"; }, function () {});
      return true;
    }
    if ((g = t.closest("[data-sel]"))) {
      var id = g.getAttribute("data-sel"), i = selected.indexOf(id);
      if (i >= 0) selected.splice(i, 1); else selected.push(id);
      saveSel(); renderHome(); renderSelbar(); return true;
    }
    if ((g = t.closest("[data-act]"))) {
      var act = g.getAttribute("data-act");
      if (act === "clear-sel") { selected = []; saveSel(); renderHome(); renderSelbar(); }
      if (act === "cmp" && selected.length) location.hash = selected.length > 1 ? "#/cmp/" + selected.map(encodeURIComponent).join(",") : "#/p/" + encodeURIComponent(selected[0]);
      return true;
    }
    if ((g = t.closest("[data-open]"))) { location.hash = "#/p/" + encodeURIComponent(g.getAttribute("data-open")); return true; }
    st = stateFor(t.closest("[data-pid]") || t);
    var inDrawer = drawer.contains(t);
    if (inDrawer) st = drawerState || st;
    if ((g = t.closest("[data-open-node]"))) { var p = g.getAttribute("data-open-node").split("|"); drawerState = st; nodeDrawer(st, p[0], p[1]); return true; }
    if ((g = t.closest("[data-open-ms]"))) { drawerState = st; msDrawer(st, g.getAttribute("data-open-ms")); return true; }
    if (!st || inDrawer) return false;
    drawerState = st;
    if ((g = t.closest("[data-expand]"))) { var xk = g.getAttribute("data-expand"); expanded[xk] = !expanded[xk]; render(); return true; }
    if ((g = t.closest("[data-line-detail]"))) { lineDrawer(st, g.getAttribute("data-line-detail")); return true; }
    if ((g = t.closest(".wchip, .wcard"))) { workerDrawer(g); return true; }
    if ((g = t.closest(".bead")) || (g = t.closest("li[data-node]"))) { nodeDrawer(st, g.getAttribute("data-line"), g.getAttribute("data-node")); return true; }
    if ((g = t.closest("[data-goal]"))) { goalDrawer(st); return true; }
    if ((g = t.closest("[data-ms]"))) { msDrawer(st, g.getAttribute("data-ms")); return true; }
    if ((g = t.closest("[data-href]"))) { location.hash = g.getAttribute("data-href").replace(/^#/, ""); return true; }
    return false;
  }
  var drawerState = null;
  document.addEventListener("click", function (ev) { if (ev.target.closest && activate(ev.target)) ev.preventDefault(); });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") { closeDrawer(); hideTip(); clearLinks(); return; }
    if ((ev.key === "Enter" || ev.key === " ") && ev.target.closest && ev.target.matches("[tabindex], [role=button], [role=link]")) {
      if (activate(ev.target)) ev.preventDefault();
    }
  });
  document.addEventListener("focusin", function (ev) {
    var t = ev.target, r;
    if (!t.closest) return;
    if (t.matches(".bead, .track-svg .station, .wchip, .wcard")) {
      r = t.getBoundingClientRect();
      var fake = { clientX: r.right, clientY: r.bottom };
      if (t.matches(".bead")) hoverBead(t, fake); else if (t.matches(".wchip, .wcard")) hoverWorker(t, fake); else hoverStation(t, fake);
    }
  });
  document.addEventListener("toggle", function (ev) {
    var d = ev.target;
    if (d && d.getAttribute && d.getAttribute("data-gid")) openGroups[d.getAttribute("data-gid")] = d.open;
  }, true);
  scrim.addEventListener("click", closeDrawer);
  document.getElementById("drawer-x").addEventListener("click", closeDrawer);

  // ----------------------------------------------------------- theme
  document.getElementById("theme-btn").addEventListener("click", function () {
    var cur = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", cur);
    try { localStorage.setItem("pb-theme", cur); } catch (e) { /* ignore */ }
  });

  // ----------------------------------------------------------- live updates
  var es = null, retry = 1000, pending = null;
  function setConn(s, label) { conn.setAttribute("data-state", s); conn.querySelector("b").textContent = label; }
  function connect() {
    if (!window.EventSource) { setConn("down", "不支持实时"); return; }
    try { es = new EventSource("api/stream"); } catch (e) { setConn("down", "实时断开"); return later(); }
    es.addEventListener("open", function () { retry = 1000; setConn("open", "实时"); });
    es.addEventListener("hello", function () { setConn("open", "实时"); });
    es.addEventListener("change", function (m) {
      var pid = null;
      try { pid = JSON.parse(m.data).project; } catch (e) { /* ignore */ }
      if (view.name === "home" || view.ids.indexOf(pid) >= 0) {
        clearTimeout(pending);
        pending = setTimeout(refresh, 300);
      }
    });
    es.addEventListener("error", function () {
      if (es.readyState === 2) { setConn("down", "实时断开"); es.close(); later(); }
      else setConn("retry", "重连中");
    });
  }
  function later() {
    setTimeout(function () { setConn("retry", "重连中"); connect(); refresh(); }, retry);
    retry = Math.min(retry * 2, 30000);
  }
  // relative times and staleness keep moving even without new events
  setInterval(function () { if (SNAP) return; if (!drawer.classList.contains("open") && tip.hidden) refresh(); }, 60000);
  var rz = null;
  window.addEventListener("resize", function () { clearTimeout(rz); rz = setTimeout(function () { if (view.name !== "home") render(); }, 150); });
  window.addEventListener("hashchange", route);

  // ?snap=1 renders once without the live stream (headless screenshots never settle with an open stream)
  var SNAP = /[?&]snap=1/.test(location.search);
  route();
  setTimeout(function () { cache._feedPrimed = true; }, 1500);
  if (SNAP) {
    setConn("retry", "快照模式");
    document.documentElement.classList.add("snap");
  } else connect();
})();
