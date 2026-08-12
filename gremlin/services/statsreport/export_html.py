#!/usr/bin/env python3
"""
Интерактивный отчёт одним HTML-файлом (открывается в браузере, ничего не требует).

    python export_html.py --db chat_stats.db --out report.html
"""

import json
from datetime import datetime, timezone

from .report_data import load_rows

TPL = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Аудит чата — %%TITLE%%</title>
<style>
:root{
  --bg:#eef0f3; --surface:#fff; --ink:#1b2430; --muted:#6b7683; --line:#d5dae1;
  --cut:#a8322d; --watch:#b0761a; --keep:#1f6f5c; --calm:#8a94a0;
  --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
  --sans:"Inter Tight","Inter","Helvetica Neue",Arial,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 var(--sans)}
header{padding:22px 24px 14px;border-bottom:1px solid var(--line);background:var(--surface)}
h1{margin:0;font-size:19px;font-weight:650;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:12px;margin-top:4px;font-family:var(--mono)}
.rail{position:sticky;top:0;z-index:5;background:var(--surface);
      border-bottom:1px solid var(--line);padding:12px 24px;
      display:flex;gap:22px;flex-wrap:wrap;align-items:flex-end}
.ctl{display:flex;flex-direction:column;gap:4px}
.ctl label{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.ctl input[type=number],.ctl input[type=search]{font:13px var(--mono);padding:6px 8px;
  border:1px solid var(--line);border-radius:4px;background:var(--bg);color:var(--ink);width:96px}
.ctl input[type=search]{width:200px}
.ctl.check{flex-direction:row;align-items:center;gap:6px}
.ctl.check label{text-transform:none;letter-spacing:0;font-size:12px;color:var(--ink)}
.chips{display:flex;gap:8px;flex-wrap:wrap;padding:14px 24px 0}
.chip{border:1px solid var(--line);background:var(--surface);border-radius:999px;
  padding:6px 13px;font-size:12px;cursor:pointer;display:flex;gap:7px;align-items:center}
.chip[aria-pressed=true]{border-color:var(--ink);background:var(--ink);color:#fff}
.chip b{font-family:var(--mono);font-weight:600}
.dot{width:7px;height:7px;border-radius:50%}
.wrap{padding:14px 24px 60px}
table{width:100%;border-collapse:collapse;background:var(--surface);
  border:1px solid var(--line);border-radius:6px;overflow:hidden}
th{position:sticky;top:57px;background:#e6e9ee;text-align:left;font-size:11px;
  text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
  padding:9px 10px;border-bottom:1px solid var(--line);cursor:pointer;white-space:nowrap;user-select:none}
th:hover{color:var(--ink)}
th.sorted::after{content:" ▾";font-size:10px}
th.sorted.asc::after{content:" ▴"}
td{padding:7px 10px;border-bottom:1px solid #eceef1;vertical-align:middle}
td.n{font-family:var(--mono);text-align:right;font-size:12.5px}
tbody tr:hover{background:#f6f8fa}
.name{font-weight:550}
.uname{font-family:var(--mono);font-size:12px;color:var(--muted)}
.pill{font-size:11.5px;padding:3px 9px;border-radius:4px;white-space:nowrap;
  font-weight:550;display:inline-block}
.p-cut{background:#f6e2e0;color:var(--cut)}
.p-watch{background:#f7ecd8;color:var(--watch)}
.p-keep{background:#e0eeea;color:var(--keep)}
.p-calm{background:#e9ebee;color:var(--calm)}
.spark{display:flex;align-items:flex-end;gap:1px;height:22px;width:118px}
.spark i{flex:1;background:var(--calm);min-height:1px;border-radius:1px;opacity:.75}
.spark i.hot{background:var(--keep);opacity:1}
.bar{display:flex;gap:10px;align-items:center;margin:0 0 12px;flex-wrap:wrap}
button.act{font:13px var(--sans);padding:7px 14px;border:1px solid var(--line);
  background:var(--surface);border-radius:5px;cursor:pointer}
button.act:hover{border-color:var(--ink)}
button.act:disabled{opacity:.45;cursor:default}
.count{font-family:var(--mono);font-size:12px;color:var(--muted)}
a{color:var(--ink)}
:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
@media (prefers-reduced-motion:no-preference){tbody tr{transition:background .12s}}
@media(max-width:800px){.spark,th.opt,td.opt{display:none}.rail{gap:14px}}
</style></head><body>
<header>
  <h1>Аудит участников — %%TITLE%%</h1>
  <div class="sub">%%SUB%%</div>
</header>

<div class="rail">
  <div class="ctl"><label for="silent">Молчит дольше, дн.</label>
    <input type="number" id="silent" value="%%SILENT%%" min="0" step="10"></div>
  <div class="ctl"><label for="minmsgs">Минимум сообщений</label>
    <input type="number" id="minmsgs" value="%%MINMSGS%%" min="0"></div>
  <div class="ctl"><label for="grace">Фора новичкам, дн.</label>
    <input type="number" id="grace" value="%%GRACE%%" min="0"></div>
  <div class="ctl"><label for="q">Поиск</label>
    <input type="search" id="q" placeholder="имя или @username"></div>
  <div class="ctl check"><input type="checkbox" id="protAdm" checked>
    <label for="protAdm">не трогать админов</label></div>
  <div class="ctl check"><input type="checkbox" id="protBot" checked>
    <label for="protBot">не трогать ботов</label></div>
  <div class="ctl check"><input type="checkbox" id="hideLeft" checked>
    <label for="hideLeft">скрыть вышедших</label></div>
</div>

<div class="chips" id="chips"></div>

<div class="wrap">
  <div class="bar">
    <button class="act" id="copy">Скопировать @username выбранных</button>
    <button class="act" id="csv">Скачать CSV</button>
    <button class="act" id="selall">Выделить всех кандидатов</button>
    <span class="count" id="count"></span>
  </div>
  <table>
    <thead><tr>
      <th style="width:28px"><input type="checkbox" id="chkAll" aria-label="выделить всё"></th>
      <th data-k="name">Участник</th>
      <th data-k="verdict">Вердикт</th>
      <th data-k="msgs" class="n">Сообщений</th>
      <th data-k="days_silent" class="n">Молчит, дн.</th>
      <th data-k="last_msg" class="opt">Последнее</th>
      <th data-k="m30" class="n opt">30 дн.</th>
      <th data-k="active_days" class="n opt">Акт. дней</th>
      <th data-k="days_in_chat" class="n opt">В чате, дн.</th>
      <th class="opt">Активность за год</th>
    </tr></thead>
    <tbody id="tb"></tbody>
  </table>
</div>

<script>
const DATA = %%DATA%%;
const SEG = [
  ["cut",   "Кандидаты на удаление", "var(--cut)"],
  ["watch", "Под наблюдением",       "var(--watch)"],
  ["keep",  "Активные",              "var(--keep)"],
  ["calm",  "Не трогать",            "var(--calm)"],
];
let sortKey = "msgs", sortAsc = false, seg = null;
const sel = new Set();
const $ = s => document.querySelector(s);
const num = v => (v === "" || v === null) ? null : Number(v);

function classify(r){
  const silent = +$("#silent").value, minm = +$("#minmsgs").value, grace = +$("#grace").value;
  if (r.role === "Уже вышел" || r.role === "Удалённый аккаунт")
    return ["calm", "Уже не в чате"];
  if ($("#protAdm").checked && r.role === "Админ") return ["calm", "Админ"];
  if ($("#protBot").checked && (r.role === "Бот" || r.role === "Аноним/канал"))
    return ["calm", r.role];
  const dic = num(r.days_in_chat);
  if (dic !== null && dic < grace) return ["calm", "Новичок, " + dic + " дн. в чате"];
  if (r.msgs === 0) return ["cut", "Ни одного сообщения"];
  const ds = num(r.days_silent);
  if (ds !== null && ds > silent) return ["cut", "Молчит " + ds + " дн."];
  if (r.msgs < minm) return ["watch", "Всего " + r.msgs + " сообщ."];
  return ["keep", "Оставить"];
}

function rows(){
  const q = $("#q").value.trim().toLowerCase();
  const hideLeft = $("#hideLeft").checked;
  return DATA.map(r => {
    const [s, why] = classify(r);
    return Object.assign({}, r, {seg: s, verdict: why});
  }).filter(r => {
    if (hideLeft && (r.role === "Уже вышел" || r.role === "Удалённый аккаунт")) return false;
    if (seg && r.seg !== seg) return false;
    if (q && !((r.name + " " + r.username).toLowerCase().includes(q))) return false;
    return true;
  }).sort((a, b) => {
    let x = a[sortKey], y = b[sortKey];
    if (x === "" || x === null) x = sortAsc ? Infinity : -Infinity;
    if (y === "" || y === null) y = sortAsc ? Infinity : -Infinity;
    if (typeof x === "string") return sortAsc ? x.localeCompare(y) : y.localeCompare(x);
    return sortAsc ? x - y : y - x;
  });
}

function spark(w){
  if (!w || !w.length) return '<div class="spark"></div>';
  const mx = Math.max(...w, 1);
  return '<div class="spark" title="понедельно за последний год">' +
    w.map(v => '<i class="' + (v ? 'hot' : '') + '" style="height:' +
      (v ? Math.max(2, Math.round(v / mx * 22)) : 1) + 'px"></i>').join("") + '</div>';
}

function render(){
  const rs = rows();
  const all = DATA.map(r => classify(r)[0]);
  $("#chips").innerHTML = SEG.map(([k, label, color]) => {
    const n = all.filter(x => x === k).length;
    return '<button class="chip" data-seg="' + k + '" aria-pressed="' + (seg === k) +
      '"><span class="dot" style="background:' + color + '"></span>' + label +
      ' <b>' + n + '</b></button>';
  }).join("") + '<button class="chip" data-seg="" aria-pressed="' + (seg === null) +
    '">Все <b>' + DATA.length + '</b></button>';

  $("#tb").innerHTML = rs.map(r => {
    const link = r.last_link ? '<a href="' + r.last_link + '" target="_blank" rel="noopener">' +
      (r.last_msg || "—") + '</a>' : (r.last_msg || "—");
    return '<tr><td><input type="checkbox" data-id="' + r.user_id + '"' +
      (sel.has(r.user_id) ? " checked" : "") + '></td>' +
      '<td><div class="name">' + esc(r.name) + '</div>' +
      '<div class="uname">' + esc(r.username || "—") + ' · ' + esc(r.role) + '</div></td>' +
      '<td><span class="pill p-' + r.seg + '">' + esc(r.verdict) + '</span></td>' +
      '<td class="n">' + r.msgs + '</td>' +
      '<td class="n">' + (r.days_silent === "" ? "—" : r.days_silent) + '</td>' +
      '<td class="opt">' + link + '</td>' +
      '<td class="n opt">' + r.m30 + '</td>' +
      '<td class="n opt">' + r.active_days + '</td>' +
      '<td class="n opt">' + (r.days_in_chat === "" ? "—" : r.days_in_chat) + '</td>' +
      '<td class="opt">' + spark(r.weeks) + '</td></tr>';
  }).join("");

  document.querySelectorAll("th[data-k]").forEach(th => {
    th.classList.toggle("sorted", th.dataset.k === sortKey);
    th.classList.toggle("asc", th.dataset.k === sortKey && sortAsc);
  });
  $("#count").textContent = "показано " + rs.length + " · выбрано " + sel.size;
}

function esc(s){ return String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

document.addEventListener("input", e => {
  if (e.target.closest(".rail")) render();
});
document.addEventListener("click", e => {
  const chip = e.target.closest(".chip");
  if (chip){ seg = chip.dataset.seg || null; render(); return; }
  const th = e.target.closest("th[data-k]");
  if (th){
    const k = th.dataset.k;
    sortAsc = (k === sortKey) ? !sortAsc : (k === "name" || k === "verdict");
    sortKey = k; render(); return;
  }
  const cb = e.target.closest("input[data-id]");
  if (cb){
    const id = +cb.dataset.id;
    cb.checked ? sel.add(id) : sel.delete(id);
    $("#count").textContent = $("#count").textContent.replace(/выбрано \d+/, "выбрано " + sel.size);
  }
});
$("#chkAll").addEventListener("change", e => {
  rows().forEach(r => e.target.checked ? sel.add(r.user_id) : sel.delete(r.user_id));
  render();
});
$("#selall").addEventListener("click", () => {
  rows().filter(r => r.seg === "cut").forEach(r => sel.add(r.user_id));
  render();
});
$("#copy").addEventListener("click", () => {
  const list = rows().filter(r => sel.has(r.user_id))
    .map(r => r.username || ("id" + r.user_id)).join("\n");
  navigator.clipboard.writeText(list).then(() => {
    $("#copy").textContent = "Скопировано ✓";
    setTimeout(() => $("#copy").textContent = "Скопировать @username выбранных", 1400);
  });
});
$("#csv").addEventListener("click", () => {
  const rs = rows().filter(r => !sel.size || sel.has(r.user_id));
  const cols = ["user_id","username","name","role","verdict","msgs","last_msg",
                "days_silent","m30","active_days","days_in_chat","joined"];
  const csv = [cols.join(",")].concat(rs.map(r => cols.map(c =>
    '"' + String(r[c] ?? "").replace(/"/g, '""') + '"').join(","))).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["\ufeff" + csv], {type:"text/csv"}));
  a.download = "chat_audit.csv"; a.click();
});
render();
</script></body></html>
"""


def build(db, out, silent_days=90, min_msgs=5, grace_days=14):
    """Собирает HTML-отчёт из базы. Возвращает (путь, число строк)."""
    rows, meta = load_rows(db)
    if not rows:
        raise RuntimeError("В базе пусто — сначала запустите сбор")

    total_msgs = sum(r["msgs"] for r in rows)
    sub = (f"{len(rows)} записей · {total_msgs:,} сообщений · "
           f"собрано {meta.get('updated_at', '')[:19].replace('T', ' ')} UTC · "
           f"отчёт сгенерирован {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"
           ).replace(",", " ")

    html = (TPL
            .replace("%%TITLE%%", meta.get("chat_title", "чат"))
            .replace("%%SUB%%", sub)
            .replace("%%SILENT%%", str(silent_days))
            .replace("%%MINMSGS%%", str(min_msgs))
            .replace("%%GRACE%%", str(grace_days))
            .replace("%%DATA%%", json.dumps(rows, ensure_ascii=False)))
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out, len(rows)
