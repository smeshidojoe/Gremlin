/* Панель Gremlin: то же меню бота, но страницей.
 *
 * Устройство простое и намеренно без сборки: маршрут лежит в hash, каждая
 * страница — функция, которая сходила в API и вернула html. Обработчики не
 * навешиваем поштучно: один слушатель на документ разбирает data-act.
 */
'use strict';

const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }

const $app = document.getElementById('app');
const $title = document.getElementById('title');
const $back = document.getElementById('back');
const $spin = document.getElementById('spin');

let INIT = null;          // ответ /api/init: кто мы и какие у нас чаты
const CACHE = {};         // мелкие данные текущей страницы, чтобы не ходить дважды

/* ---------- helpers ---------- */

const esc = (s) => String(s === null || s === undefined ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const plain = (s) => String(s || '').replace(/<[^>]+>/g, '');

function num(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

let busy = 0;
function spin(on) {
  busy += on ? 1 : -1;
  $spin.hidden = busy <= 0;
}

async function api(path, opts = {}) {
  const headers = { 'X-Init-Data': (tg && tg.initData) || '' };
  let body;
  if (opts.form) {
    body = opts.form;                       // FormData сам проставит границы
  } else if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.json);
  }
  spin(true);
  try {
    const r = await fetch('/api' + path, { method: opts.method || (body ? 'POST' : 'GET'), headers, body });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || ('Ошибка ' + r.status));
    return data;
  } finally {
    spin(false);
  }
}

function toast(text) {
  const el = document.getElementById('toast');
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 2600);
}

function haptic(kind) {
  try { tg.HapticFeedback.impactOccurred(kind || 'light'); } catch (e) { /* не всякий клиент умеет */ }
}

function confirmAsk(text) {
  return new Promise((resolve) => {
    if (tg && tg.showConfirm) tg.showConfirm(text, resolve);
    else resolve(window.confirm(text));
  });
}

/* Модалка ввода: одно поле, кнопки «Сохранить» и «Отмена». */
function ask({ title, hint, value = '', multiline = false, placeholder = '', ok = 'Сохранить' }) {
  return new Promise((resolve) => {
    const box = document.getElementById('modal');
    box.innerHTML = `
      <div class="sheet">
        <h3>${esc(title)}</h3>
        ${hint ? `<div class="hint">${hint}</div>` : ''}
        ${multiline
          ? `<textarea id="ask-input" placeholder="${esc(placeholder)}">${esc(value)}</textarea>`
          : `<input id="ask-input" type="text" value="${esc(value)}" placeholder="${esc(placeholder)}">`}
        <div class="btns">
          <button class="btn ghost" data-modal="cancel">Отмена</button>
          <button class="btn" data-modal="ok">${esc(ok)}</button>
        </div>
      </div>`;
    box.hidden = false;
    const input = document.getElementById('ask-input');
    input.focus();
    const close = (val) => { box.hidden = true; box.innerHTML = ''; resolve(val); };
    box.onclick = (e) => {
      if (e.target === box) return close(null);
      const act = e.target.dataset.modal;
      if (act === 'cancel') close(null);
      if (act === 'ok') close(input.value);
    };
    if (!multiline) input.onkeydown = (e) => { if (e.key === 'Enter') close(input.value); };
  });
}

/* Выбор одного значения из списка — вместо селектора ◀ ▶ из меню бота. */
function pick({ title, options, value }) {
  return new Promise((resolve) => {
    const box = document.getElementById('modal');
    box.innerHTML = `
      <div class="sheet">
        <h3>${esc(title)}</h3>
        <div class="wrap" style="margin-top:8px">
          ${options.map((o) => `<button class="chip ${o.value === value ? 'on' : ''}"
             data-pick="${esc(String(o.value))}">${esc(o.label)}</button>`).join('')}
        </div>
        <div class="btns"><button class="btn ghost" data-pick-cancel>Отмена</button></div>
      </div>`;
    box.hidden = false;
    const close = (val) => { box.hidden = true; box.innerHTML = ''; resolve(val); };
    box.onclick = (e) => {
      if (e.target === box || e.target.hasAttribute('data-pick-cancel')) return close(null);
      const b = e.target.closest('[data-pick]');
      if (b) close(b.dataset.pick);
    };
  });
}

/* ---------- сборка кусочков разметки ---------- */

const tile = (href, label, opts = {}) => `
  <button class="tile" data-go="${esc(href)}">
    ${opts.dot === undefined ? '' : `<span class="dot ${opts.dot ? 'on' : 'off'}"></span>`}
    <span>${esc(label)}${opts.sub ? `<small>${esc(opts.sub)}</small>` : ''}</span>
  </button>`;

// Кучка — это похожие друг на друга улики, собранные вместе; своей метки
// у неё нет, метки есть у каждой улики внутри. Пишем предложением: голые
// числа рядом с «9 шт» читались как непонятно что.
const clusterState = (g) => {
  if (!g.spam && !g.ok) return 'ни одна улика ещё не размечена';
  const parts = [];
  if (g.spam) parts.push(`⛔ спамом — ${g.spam}`);
  if (g.ok) parts.push(`🕊 нормой — ${g.ok}`);
  if (g.unknown) parts.push(`✋ без оценки — ${g.unknown}`);
  return 'из них помечено: ' + parts.join(', ');
};

// Подсветка вкладки живёт в разметке, поэтому переставляем её сами:
// раньше «Без оценки» горела всегда, что бы ни было открыто.
const markTab = (tab) => {
  const box = document.getElementById('cluster-tabs');
  if (!box) return;
  box.querySelectorAll('[data-tab]').forEach((b) => {
    b.classList.toggle('ghost', b.dataset.tab !== tab);
  });
};

const switchRow = (key, label, on, extra = '') => `
  <div class="row">
    <div class="label">${esc(label)}${extra ? `<small>${esc(extra)}</small>` : ''}</div>
    <label class="switch">
      <input type="checkbox" data-toggle="${esc(key)}" ${on ? 'checked' : ''}>
      <span></span>
    </label>
  </div>`;

const selectRow = (key, label, value, options) => `
  <div class="row">
    <div class="label">${esc(label)}</div>
    <select data-select="${esc(key)}">
      ${options.map((o) => `<option value="${esc(String(o.value))}"
        ${String(o.value) === String(value) ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}
    </select>
  </div>`;

const linkRow = (href, label, value) => `
  <button class="row" style="width:100%;background:none;border:0;color:inherit;font:inherit;text-align:left;cursor:pointer"
          data-go="${esc(href)}">
    <div class="label">${esc(label)}</div>
    <div class="value">${esc(value === undefined ? '' : value)} ›</div>
  </button>`;

const chips = (list, act) => `<div class="wrap">${list.map((b) => `
  <button class="chip ${b.on ? 'on' : 'off'}" data-act="${act}" data-bit="${b.bit}">
    ${b.on ? '✓' : '○'} ${esc(b.label)}</button>`).join('')}</div>`;

/* Разложить по владельцам: [{owner, items}], владельцы по алфавиту,
   свои чаты — первыми, чтобы не искать их среди чужих. */
function groupByOwner(items, mineId) {
  const mine = mineId || (INIT.user && INIT.user.id);
  const byOwner = new Map();
  for (const it of items) {
    const key = it.owner_id || 0;
    if (!byOwner.has(key)) byOwner.set(key, { owner: it.owner || 'без владельца', items: [] });
    byOwner.get(key).items.push(it);
  }
  return [...byOwner.entries()]
    .sort((a, b) => (a[0] === mine ? -1 : b[0] === mine ? 1
      : a[1].owner.localeCompare(b[1].owner, 'ru')))
    .map(([, g]) => g);
}

/* ---------- страницы ---------- */

async function homeView() {
  const d = INIT;
  const tileFor = (c) => `
    <button class="tile" data-go="#/chat/${c.chat_id}" style="grid-column:1/-1">
      <span>${esc(c.title)}${c.linked ? `<small>📣 ${esc(c.linked)}</small>` : ''}</span>
      <span class="right muted">›</span>
    </button>`;
  // у владельца бота в списке чаты разных людей — группируем по хозяину,
  // иначе список превращается в кашу
  const chats = d.owner ? groupByOwner(d.chats).map((g) => `
      <div class="label" style="margin:10px 0 4px">👤 ${esc(g.owner)}
        <small>${g.items.length} ${num(g.items.length, 'чат', 'чата', 'чатов')}</small></div>
      <div class="tiles">${g.items.map(tileFor).join('')}</div>`).join('')
    : `<div class="tiles">${d.chats.map(tileFor).join('')}</div>`;

  const ownerTiles = d.owner ? `
    <h2>Владельцу бота</h2>
    <div class="tiles">
      ${tile('#/access', '👥 Доступ к боту')}
      ${tile('#/seed', '🌱 Стартовый набор')}
      ${tile('#/roulette', '🎯 Бан-рулетка')}
      ${tile('#/admin/log', '📜 Лог событий')}
      ${tile('#/admin/errors', '🐞 Ошибки')}
      ${tile('#/admin/health', '⚙️ Состояние')}
      <button class="tile" data-act="global-log">
        <span>🌍 Глобальный лог<small>${esc((d.global_log && d.global_log.title) || 'не задан')}</small></span>
      </button>
    </div>` : '';

  return {
    title: 'Gremlin',
    html: `
      <div class="card">
        <h2>💬 Чаты <span class="muted">(${d.chats.length})</span></h2>
        ${d.chats.length ? chats
                : '<div class="empty">Пока пусто. Добавьте бота администратором в свой чат.</div>'}
        <div style="margin-top:10px"><a class="btn wide" href="${esc(d.add_url)}" target="_blank" rel="noopener">➕ Добавить в чат</a></div>
      </div>
      <div class="card">
        <div class="tiles">
          ${tile('#/nets', '🕸 Сетки чатов', { sub: d.nets + ' ' + num(d.nets, 'сетка', 'сетки', 'сеток') })}
          ${tile('#/help', 'ℹ️ О панели')}
        </div>
      </div>
      ${ownerTiles ? `<div class="card">${ownerTiles}</div>` : ''}`,
  };
}

async function helpView() {
  return {
    title: 'О панели',
    back: '#/',
    html: `<div class="card intro">
      Панель показывает всё то же, что меню бота: разделы, списки, наказания и сетки.
      Меняется всё сразу — бот подхватывает настройки на лету.<br><br>
      Медиа-ответы триггеров можно загружать и здесь (кнопка «Загрузить файл» в вариантах ответа),
      и по-старому в переписке с ботом.<br><br>
      Панель открыта только тем, кому открыт бот, и показывает лишь ваши чаты.
    </div>`,
  };
}

async function chatView(cid) {
  const d = await api(`/chat/${cid}`);
  CACHE.chat = d;
  const st = d.stats;
  const setup = d.needs_setup ? `
    <div class="card">
      <h2>🆕 Чат ещё не настраивали</h2>
      <div class="intro">Можно перенести правила из другого чата — фильтры, стоп-слова,
        вайтлисты, триггеры и счётчики поедут целиком, вместе с медиа.</div>
      <div class="wrap" style="margin-top:10px">
        <button class="btn" data-go="#/chat/${cid}/copy">📥 Перенести настройки</button>
        <button class="btn ghost" data-act="setup-skip">🛠 С нуля</button>
      </div>
    </div>` : '';

  const overview = d.overview.map((o) =>
    `<span class="chip stat ${o.on ? 'on' : 'off'}">${o.on ? '✓' : '○'} ${esc(o.label)}</span>`).join('');

  return {
    title: d.chat.title,
    back: '#/',
    html: `
      ${setup}
      <div class="card">
        <h2>${esc(d.chat.title)}</h2>
        <div class="muted mono">${esc(d.chat.chat_id)}</div>
        ${d.chat.owner_name ? `<div class="muted">👤 Владелец: ${esc(d.chat.owner_name)}</div>` : ''}
        <div class="row"><div class="label">💬 Сообщений</div>
          <div class="value">сегодня ${st.d1} · за 7д ${st.d7}</div></div>
        <div class="row"><div class="label">👥 За 7 дней</div>
          <div class="value">пришло ${st.joins} · ушло ${st.leaves}</div></div>
        <div class="row"><div class="label">🔨 Наказаний</div>
          <div class="value">активных ${d.active} · за 7д ${st.pun7}</div></div>
        <div style="margin-top:10px" class="wrap">${overview}</div>
      </div>

      <div class="card">
        <div class="tiles">
          ${d.sections.map((s) => tile(`#/chat/${cid}/s/${s.key}`, s.title,
            s.on === null ? {} : { dot: s.on })).join('')}
          ${tile(`#/chat/${cid}/games`, '🎪 Приколы', { dot: d.games_on })}
        </div>
      </div>

      <div class="card">
        <div class="tiles">
          ${tile(`#/chat/${cid}/active`, '🚫 Наказания', { sub: 'активных ' + d.active })}
          ${tile(`#/chat/${cid}/stats`, '📈 Статистика')}
          ${tile(`#/chat/${cid}/events`, '📜 Лог чата')}
          ${tile(`#/chat/${cid}/copy`, '📥 Перенести настройки')}
          <button class="tile" data-act="set-log">
            <span>📍 Лог-чат<small>${esc(d.log_chat.title || 'не задан')}</small></span></button>
          <button class="tile" data-act="chat-net">
            <span>🕸 Сетка<small>${esc(d.net ? d.net.title : 'нет')}</small></span></button>
          <button class="tile" data-act="leave" style="grid-column:1/-1">
            <span>🚪 Убрать бота из чата</span></button>
        </div>
      </div>`,
  };
}

/* --- раздел настроек --- */

async function sectionView(cid, sec) {
  const d = await api(`/chat/${cid}/section/${sec}`);
  const fields = d.fields.filter((f) => f.visible).map((f) => (
    f.kind === 'toggle'
      ? switchRow(f.key, f.label, !!f.value)
      : selectRow(f.key, f.label, f.value, f.options)
  )).join('');

  const widgets = d.widgets.map((w) => widgetHtml(w, d.widget_data[w] || {}, cid, d)).join('');
  // back в схеме — либо ключ раздела, либо готовый callback меню; у наказаний
  // это «u:p:{cid}:0», и на странице ему соответствует список наказаний
  const back = !d.back ? `#/chat/${cid}`
    : d.back.startsWith('u:p:') ? `#/chat/${cid}/active`
    : d.back.startsWith('u:') ? `#/chat/${cid}`
    : `#/chat/${cid}/s/${d.back}`;

  return {
    title: plain(d.title),
    back,
    html: `
      <div class="card"><div class="intro">${d.intro}</div></div>
      ${fields ? `<div class="card">${fields}</div>` : ''}
      ${widgets}`,
  };
}

function widgetHtml(name, w, cid, d) {
  switch (name) {
    case 'anon':
      return `<div class="card">${linkRow(`#/chat/${cid}/s/wl`, '🕊 Разрешённые отправители', w.count)}</div>`;

    case 'links_pun':
      return `<div class="card"><div class="tiles">
        ${tile(`#/chat/${cid}/s/links_member`, '⚖️ Наказания участникам')}
        ${tile(`#/chat/${cid}/s/links_guest`, '⚖️ Наказания не участникам')}
      </div></div>`;

    case 'link_wl':
      return `<div class="card">${linkRow(`#/chat/${cid}/linkwl`, '🔓 Разрешённые чаты и каналы', w.count)}</div>`;

    case 'inline_wl':
      return `<div class="card">
        <h2>🤖 Разрешённые боты</h2>
        ${(w.items || []).map((r) => `<div class="item">
            <div class="body">@${esc(r.username)}</div>
            <button class="x" data-act="inlinewl-del" data-id="${r.id}">✕</button>
          </div>`).join('') || '<div class="empty">Пусто.</div>'}
        <button class="btn wide" style="margin-top:10px" data-act="inlinewl-add">➕ Разрешить бота</button>
      </div>`;

    case 'words':
      return `<div class="card">
        ${linkRow(`#/chat/${cid}/words`, '📝 Список слов', w.count)}
        <button class="btn wide" style="margin-top:10px" data-act="words-add">➕ Добавить слова</button>
      </div>`;

    case 'wl':
      return `<div class="card">
        <h2>🕊 Вайтлист</h2>
        ${(w.items || []).map((e) => `<button class="item" style="width:100%;background:none;border:0;color:inherit;font:inherit;text-align:left"
            data-go="#/chat/${cid}/wl/${e.row_id}">
            <div class="body">${esc(e.who)}<small>${esc(e.label)}</small></div>
            <div class="value">›</div>
          </button>`).join('') || '<div class="empty">Пусто.</div>'}
        <button class="btn wide" style="margin-top:10px" data-act="wl-add">➕ Добавить</button>
      </div>`;

    case 'logsel':
      return `<div class="card">
        <div class="row"><div class="label">📍 Лог-чат<small>${esc(w.title || 'не задан')}</small></div>
          <button class="btn small" data-act="set-log">Изменить</button></div>
      </div>`;

    case 'phrases':
      return `<div class="card">
        <h2>🧠 Фразы-образцы</h2>
        <div class="muted">Бот ловит сообщения, похожие по смыслу на эти фразы,
          даже если ни одно слово не совпало.</div>
        ${w.items.map((r) => `
          <div class="row">
            <div class="label">${esc(r.text)}<small>поймала ${r.hits}</small></div>
            <button class="x" data-act="phrase-del" data-id="${r.id}">✕</button>
          </div>`).join('') || '<div class="empty">Пусто.</div>'}
        <button class="btn wide" style="margin-top:10px" data-act="phrase-add">
          ➕ Добавить фразу</button>
      </div>`;

    case 'read_stats':
      return `<div class="card">
        <h2>🔍 Что бот умеет читать</h2>
        <div class="row"><div class="label">🖼 Картинки<small>tesseract в контейнере</small></div>
          <div class="value">${esc(w.ocr === 'ok' ? 'готов' : w.ocr)}</div></div>
        <div class="row"><div class="label">🔊 Голосовые<small>сторонняя служба, ASR_URL</small></div>
          <div class="value">${esc(w.asr === 'ok' ? 'подключена' : w.asr)}</div></div>
        ${w.asr_url ? '' : `<div class="muted" style="margin-top:10px">
          Служба расшифровки не задана — переключатель голосовых ничего не делает.</div>`}
      </div>`;

    case 'nn_stats':
      return `<div class="card">
        <h2>📊 Копилка улик</h2>
        <div class="row"><div class="label">Всего собрано</div>
          <div class="value">${w.total}</div></div>
        <div class="row"><div class="label">Годится для сравнения<small>без ручных наказаний</small></div>
          <div class="value">${w.profile}</div></div>
        <div class="row"><div class="label">⛔ Спам</div><div class="value">${w.spam}</div></div>
        <div class="row"><div class="label">🕊 Норма</div><div class="value">${w.ok}</div></div>
        <div class="row"><div class="label">✋ Ручные наказания<small>в сравнении не участвуют</small></div>
          <div class="value">${w.unknown}</div></div>
        <div class="row"><div class="label">🧠 Модель</div>
          <div class="value">${esc(w.model === 'ok' ? 'загружена' : w.model)}</div></div>
        <div class="row"><div class="label">📐 Как считает<small>регрессия включается
          с ${w.logreg_min} улик</small></div>
          <div class="value">${w.profile >= w.logreg_min ? 'регрессия' : 'соседи'}</div></div>
        ${w.suggest ? `<div class="row"><div class="label">🎚 Рекомендованный порог
          <small>при нём норма из копилки не срабатывает</small></div>
          <div class="value">${w.suggest}%${w.suggest === w.threshold ? ' ✅' : ''}</div></div>`
          : ''}
        ${w.profile < w.min ? `<div class="muted" style="margin-top:10px">
          Для сравнения нужно хотя бы ${w.min} улик — пока копим.</div>` : ''}
      </div>`;

    case 'nn_subs':
      // смысловые фразы и рассылки — тот же нейрофильтр, другая копилка;
      // отдельными пунктами меню они выглядели как три разных механизма
      return `<div class="card">
        ${tile(`#/chat/${cid}/s/sem`, '🧠 Смысловые стоп-слова',
               { dot: w.sem_on, sub: w.phrases + ' ' + num(w.phrases, 'фраза', 'фразы', 'фраз') })}
        ${tile(`#/chat/${cid}/s/burst`, '📡 Рассылки', { dot: w.burst_on })}
      </div>`;

    case 'watch_subs':
      return `<div class="card">
        ${tile(`#/chat/${cid}/s/prof`, '🪪 Проверка профиля', { dot: w.prof_on })}
        ${tile(`#/chat/${cid}/s/cas`, '🌐 Общий список спамеров', { dot: w.cas_on })}
      </div>`;

    case 'cas_stats':
      return `<div class="card">
        <h2>🌐 Что бот уже спрашивал</h2>
        <div class="row"><div class="label">Сервис</div>
          <div class="value">${esc(w.service)}</div></div>
        <div class="row"><div class="label">⛔ Нашлись в списке</div>
          <div class="value">${w.listed}</div></div>
        <div class="row"><div class="label">🕊 Чистые</div>
          <div class="value">${w.clean}</div></div>
      </div>`;

    case 'nn_clusters':
      return `<div class="card">
        <h2>🗂 Виды спама</h2>
        <div class="muted">Копилка раскладывается на кучки похожих улик. Разметив кучку
          целиком, вы размечаете все её улики разом — вместо сотни карточек одно нажатие.</div>
        <div class="row" style="margin-top:10px" id="cluster-tabs">
          <button class="btn ghost" data-act="nn-clusters" data-scope="unknown"
            data-tab="unknown">✋ Без оценки (${w.unknown})</button>
          <button class="btn ghost" data-act="nn-clusters" data-scope="profile"
            data-tab="profile">📊 Размеченные (${w.profile})</button>
          <button class="btn ghost" data-act="nn-doubt" data-tab="doubt">🤔 Спорное</button>
        </div>
        <div id="clusters"></div>
      </div>`;

    case 'cardbits':
      return `<div class="card"><h2>Что слать карточками</h2>${chips(w.bits, 'bit-card')}</div>`;

    case 'mediabits':
      return `<div class="card"><h2>Что удалять</h2>${chips(w.bits, 'bit-media')}</div>`;

    case 'trustbits':
      return `<div class="card"><h2>Что смягчать</h2>${chips(w.bits, 'bit-trust')}</div>`;

    case 'trustsoft':
      return `<div class="card">${linkRow(`#/chat/${cid}/s/trust_soft`, '🎚 Что смягчать', `${w.on} из ${w.total}`)}</div>`;

    case 'welcome_text':
      return `<div class="card">
        ${linkRow(`#/chat/${cid}/answers/welcome/${cid}`, '✏️ Заготовки приветствия', w.count)}
        ${w.legacy ? '<button class="btn wide ghost" style="margin-top:10px" data-act="welcome-migrate">⤴️ Перенести старый текст в заготовки</button>' : ''}
      </div>`;

    case 'rules_text':
      return `<div class="card">${linkRow(`#/chat/${cid}/answers/rules/${cid}`, '✏️ Заготовки под посты', w.count)}</div>`;

    case 'warnlist':
      return `<div class="card">${linkRow(`#/chat/${cid}/warned`, '📋 Кто с варнами', w.count)}</div>`;

    case 'trigs':
      return `<div class="card">
        ${linkRow(`#/chat/${cid}/trigs`, '📋 Список триггеров', `${w.count} из ${w.limit}`)}
        <button class="btn wide" style="margin-top:10px" data-act="trig-add">➕ Добавить триггер</button>
      </div>`;

    case 'cmds':
      return `<div class="card">
        ${linkRow(`#/chat/${cid}/cmds`, '📋 Список счётчиков', `${w.count} из ${w.limit}`)}
        <button class="btn wide" style="margin-top:10px" data-act="cmd-add">➕ Добавить счётчик</button>
      </div>`;

    case 'digest_to': {
      const state = d.digest_state;
      return `<div class="card">
        <div class="row"><div class="label">👤 Получатель<small>${esc(w.who || 'не задан')}</small></div>
          <button class="btn small" data-act="digest-to">Изменить</button></div>
        ${w.to ? `<div class="wrap" style="margin-top:10px">
            <button class="btn small" data-act="digest-now">📤 Обновить сейчас</button>
            <button class="btn small ghost" data-act="digest-off">🚫 Убрать получателя</button>
          </div>` : ''}
        ${state ? `<div class="muted" style="margin-top:10px">
            👥 Участников: ${state.members} · ${state.full ? 'молчали всю неделю' : 'пока не писали'}: ${state.silent}<br>
            неделя ${esc(state.period)} · обновлено ${esc(state.updated)}</div>`
          : '<div class="muted" style="margin-top:10px">⚠️ База статистики не найдена.</div>'}
      </div>`;
    }

    default:
      return '';
  }
}

/* --- списки --- */

async function wordsView(cid) {
  const d = await api(`/chat/${cid}/words`);
  return {
    title: 'Стоп-слова',
    back: `#/chat/${cid}/s/words`,
    html: `<div class="card">
      <div class="intro">Слово со звёздочкой ловит любые окончания.</div>
      <div style="margin-top:10px">
        ${d.items.map((r) => `<div class="item">
          <div class="body mono">${esc(r.label)}</div>
          <button class="x" data-act="word-del" data-id="${r.id}">✕</button></div>`).join('')
          || '<div class="empty">Пусто.</div>'}
      </div>
      <button class="btn wide" style="margin-top:10px" data-act="words-add">➕ Добавить слова</button>
      ${d.items.length ? '<button class="btn wide danger" style="margin-top:8px" data-act="words-clear">🗑 Очистить список</button>' : ''}
    </div>`,
  };
}

async function wlEntryView(cid, rid) {
  const d = await api(`/chat/${cid}/section/wl`);
  const e = (d.widget_data.wl.items || []).find((x) => String(x.row_id) === String(rid));
  if (!e) return { title: 'Вайтлист', back: `#/chat/${cid}/s/wl`, html: '<div class="empty">Запись пропала.</div>' };
  const scopes = (d.widget_data.wl.scopes || []).map((x) => [x.key, x.label]);
  const on = new Set(e.scopes);
  const all = on.has('all');
  return {
    title: e.who,
    back: `#/chat/${cid}/s/wl`,
    html: `<div class="card">
      <h2>🕊 ${esc(e.who)}</h2>
      <div class="muted mono">${esc(e.user_id || ('@' + (e.username || '')))}</div>
      <div class="intro" style="margin-top:8px">Отмеченное для него не проверяется.
        «Полный игнор» включает всё сразу.</div>
      <div class="wrap" style="margin-top:10px">
        ${scopes.map(([key, label]) => {
          const active = key === 'all' ? all : (all || on.has(key));
          return `<button class="chip ${active ? 'on' : 'off'}" data-act="wl-scope"
                    data-row="${e.row_id}" data-scope="${key}">${active ? '✓' : '○'} ${esc(label)}</button>`;
        }).join('')}
      </div>
      <button class="btn wide danger" style="margin-top:12px" data-act="wl-del" data-row="${e.row_id}">
        🗑 Убрать из вайтлиста</button>
    </div>`,
  };
}

async function linkwlView(cid) {
  const d = await api(`/chat/${cid}/linkwl`);
  return {
    title: 'Разрешённые чаты',
    back: `#/chat/${cid}/s/links`,
    html: `<div class="card">
      <div class="intro">Ссылки на эти чаты и каналы бот не трогает.</div>
      <div style="margin-top:10px">
        ${d.items.map((r) => `<div class="item">
            <div class="body">${esc(r.title || (r.username ? '@' + r.username : r.target_id))}
              <small class="mono">${esc(r.target_id || ('@' + (r.username || '')))}</small></div>
            <button class="x" data-act="linkwl-del" data-id="${r.id}">✕</button></div>`).join('')
          || '<div class="empty">Пусто.</div>'}
      </div>
      <button class="btn wide" style="margin-top:10px" data-act="linkwl-add">➕ Разрешить чат или канал</button>
    </div>`,
  };
}

async function trigsView(cid) {
  const d = await api(`/chat/${cid}/trigs`);
  return {
    title: 'Триггеры',
    back: `#/chat/${cid}/s/triggers`,
    html: `<div class="card">
      <div class="muted">Всего: ${d.items.length} из ${d.limit}</div>
      <div style="margin-top:8px">
        ${d.items.map((r) => `<button class="item" style="width:100%;background:none;border:0;color:inherit;font:inherit;text-align:left"
            data-go="#/chat/${cid}/trig/${r.id}">
            <div class="body">${r.answers > 1 ? '🎲' : (r.media ? '🖼' : '💬')} ${esc(r.phrase)}
              <small>вариантов: ${r.answers}</small></div>
            <div class="value">›</div></button>`).join('') || '<div class="empty">Пока ни одного.</div>'}
      </div>
      <button class="btn wide" style="margin-top:10px" data-act="trig-add">➕ Добавить триггер</button>
    </div>`,
  };
}

async function trigView(cid, rid) {
  const d = await api(`/chat/${cid}/trigs/${rid}`);
  const t = d.trigger;
  return {
    title: t.phrase,
    back: `#/chat/${cid}/trigs`,
    html: `<div class="card">
      <div class="row"><div class="label">Фраза<small class="mono">${esc(t.phrase)}</small></div>
        <button class="btn small" data-act="trig-phrase" data-id="${rid}">Изменить</button></div>
      <div class="row"><div class="label">Кулдаун</div>
        <select data-cooldown="trig" data-id="${rid}">
          ${d.cooldowns.map((c) => `<option value="${c}" ${c === t.cooldown ? 'selected' : ''}>
            ${c ? c + ' сек' : 'без кулдауна'}</option>`).join('')}
        </select></div>
      ${linkRow(`#/chat/${cid}/answers/trig/${rid}`, '🎲 Варианты ответа', d.answers.length)}
      <button class="btn wide danger" style="margin-top:12px" data-act="trig-del" data-id="${rid}">
        ❌ Удалить триггер</button>
    </div>`,
  };
}

async function cmdsView(cid) {
  const d = await api(`/chat/${cid}/cmds`);
  return {
    title: 'Счётчики',
    back: `#/chat/${cid}/s/cmds`,
    html: `<div class="card">
      <div class="muted">Всего: ${d.items.length} из ${d.limit}</div>
      <div style="margin-top:8px">
        ${d.items.map((r) => `<button class="item" style="width:100%;background:none;border:0;color:inherit;font:inherit;text-align:left"
            data-go="#/chat/${cid}/cmd/${r.id}">
            <div class="body mono">${esc(r.cmd)} <small>вызовов: ${r.count}</small></div>
            <div class="value">›</div></button>`).join('') || '<div class="empty">Пока ни одного.</div>'}
      </div>
      <button class="btn wide" style="margin-top:10px" data-act="cmd-add">➕ Добавить счётчик</button>
    </div>`,
  };
}

async function cmdView(cid, rid) {
  const d = await api(`/chat/${cid}/cmds/${rid}`);
  const c = d.cmd;
  return {
    title: c.cmd,
    back: `#/chat/${cid}/cmds`,
    html: `<div class="card">
      <div class="row"><div class="label">Вызовов</div><div class="value">${c.count}</div></div>
      <div class="row"><div class="label">Кулдаун</div>
        <select data-cooldown="cmd" data-id="${rid}">
          ${d.cooldowns.map((x) => `<option value="${x}" ${x === c.cooldown ? 'selected' : ''}>
            ${x ? x + ' сек' : 'без кулдауна'}</option>`).join('')}
        </select></div>
      ${linkRow(`#/chat/${cid}/answers/cmd/${rid}`, '🎲 Варианты ответа', d.answers.length)}
      <div class="wrap" style="margin-top:12px">
        <button class="btn ghost" data-act="cmd-reset" data-id="${rid}">🔄 Сбросить счёт</button>
        <button class="btn danger" data-act="cmd-del" data-id="${rid}">❌ Удалить</button>
      </div>
    </div>`,
  };
}

const ANS_BACK = {
  trig: (cid, oid) => `#/chat/${cid}/trig/${oid}`,
  cmd: (cid, oid) => `#/chat/${cid}/cmd/${oid}`,
  welcome: (cid) => `#/chat/${cid}/s/welcome`,
  rules: (cid) => `#/chat/${cid}/s/rules`,
};

async function answersView(cid, owner, oid) {
  const d = await api(`/chat/${cid}/answers?owner=${owner}&oid=${oid}`);
  const media = owner !== 'cmd';   // у счётчика ответ только текстовый: к нему дописывается число
  return {
    title: 'Варианты ответа',
    back: ANS_BACK[owner](cid, oid),
    html: `<div class="card">
      <div class="intro">Вариантов несколько — бот отвечает случайным.
        ${media ? 'Можно текст, медиа или медиа с подписью.' : 'Только текст: число в скобках дописывается само.'}</div>
      <div style="margin-top:10px">
        ${d.items.map((a) => `<div class="item">
            <div class="body">${a.has_media ? `🖼 медиа (${esc(a.media_type)})<br>` : ''}${esc(a.plain) || '<span class="muted">без подписи</span>'}</div>
            <button class="x" data-act="ans-del" data-id="${a.id}">✕</button></div>`).join('')
          || '<div class="empty">Пусто — бот промолчит.</div>'}
      </div>
      <div class="muted" style="margin-top:8px">Всего: ${d.items.length} из ${d.limit}</div>
      <button class="btn wide" style="margin-top:10px" data-act="ans-add"
              data-owner="${owner}" data-oid="${oid}">➕ Добавить текст</button>
      ${media ? `<label class="btn wide ghost" style="margin-top:8px">🖼 Загрузить медиа
        <input type="file" hidden data-upload="answer" data-owner="${owner}" data-oid="${oid}"></label>` : ''}
    </div>`,
  };
}

async function warnedView(cid) {
  const d = await api(`/chat/${cid}/warned`);
  return {
    title: 'Варны',
    back: `#/chat/${cid}/s/warns`,
    html: `<div class="card">
      <div class="muted">Людей с варнами: ${d.items.length} · лимит: ${d.limit}</div>
      <div style="margin-top:8px">
        ${d.items.map((r) => `<div class="item">
            <div class="body">${esc(r.who)}<small>${r.count}/${d.limit} · ${esc(r.when)}</small></div>
            <button class="btn small ghost" data-act="warn-reset" data-uid="${r.user_id}">🧹 Снять</button>
          </div>`).join('') || '<div class="empty">Пока чисто.</div>'}
      </div>
    </div>`,
  };
}

async function activeView(cid) {
  const d = await api(`/chat/${cid}/active`);
  return {
    title: 'Наказания',
    back: `#/chat/${cid}`,
    html: `<div class="card">
      <h2>📋 Активные (${d.items.length})</h2>
      <div>
        ${d.items.map((p) => `<div class="item">
            <div class="body">${esc(p.who)}<small>${esc(p.kind_label)} · ${esc(p.until)} · ${esc(p.reason)}</small></div>
            <button class="btn small ghost" data-act="lift" data-id="${p.id}">🔓 Снять</button>
          </div>`).join('') || '<div class="empty">Все чисты.</div>'}
      </div>
    </div>
    <div class="card">
      <h2>Массовые действия</h2>
      <div class="intro">Список id или @username одним полем, через пробел или запятую.</div>
      <div class="wrap" style="margin-top:10px">
        <button class="btn ghost" data-act="mass" data-kind="unban">🔓 Разбан</button>
        <button class="btn ghost" data-act="mass" data-kind="kick">👢 Кик</button>
        <button class="btn danger" data-act="mass" data-kind="ban">⛔ Бан</button>
      </div>
      <div style="margin-top:10px">${linkRow(`#/chat/${cid}/s/punish_cfg`, '⚙️ Настройки наказаний', '')}</div>
    </div>`,
  };
}

async function gamesView(cid) {
  const d = await api(`/chat/${cid}/games`);
  return {
    title: 'Приколы',
    back: `#/chat/${cid}`,
    html: `<div class="card">
      <div class="intro">Игры для этого чата. Наказания настоящие — снимаются в разделе
        «Наказания». Админов и бота игры не трогают, итоговое сообщение исчезает через 10 минут.</div>
    </div>
    ${d.items.map((g) => `<div class="card">
      <div class="row">
        <div class="label"><b>${esc(g.label)}</b><small class="mono">${esc(g.how)}</small></div>
        <label class="switch"><input type="checkbox" data-game="${g.bit}" ${g.on ? 'checked' : ''}><span></span></label>
      </div>
      <div class="intro">${esc(g.about)}</div>
      ${g.by_hand ? `<div class="wrap" style="margin-top:10px">
        <button class="chip ${g.admins ? 'on' : ''}" data-act="game-who" data-bit="${g.bit}">
          ${g.admins ? '🛡 только админы' : '👥 все'}</button>
        <button class="chip" data-act="game-kind" data-bit="${g.bit}">🔨 ${esc(g.kind === 'ban' ? 'бан' : 'мут')}</button>
        ${g.kind === 'mute' ? `<button class="chip" data-act="game-min" data-bit="${g.bit}">⏰ ${esc(g.prize.replace('мут на ', ''))}</button>` : ''}
      </div>` : ''}
    </div>`).join('')}`,
  };
}

async function copyView(cid) {
  const d = await api(`/chat/${cid}/copy`);
  CACHE.copy = { src: null, groups: new Set(d.groups.map((g) => g.key)) };
  return {
    title: 'Перенос настроек',
    back: `#/chat/${cid}`,
    html: `<div class="card">
      <div class="intro">Выберите чат-источник и разделы. Вместе с настройками едут списки:
        стоп-слова, вайтлист, разрешённые чаты и боты, триггеры с медиа, счётчики.
        Не переносятся получатель сводки и счёт вызовов.</div>
      <div style="margin-top:10px">
        ${d.chats.map((c) => `<button class="item" style="width:100%;background:none;border:0;color:inherit;font:inherit;text-align:left"
            data-act="copy-src" data-src="${c.chat_id}">
            <div class="body">${esc(c.title)}</div><div class="value" data-src-mark="${c.chat_id}">○</div>
          </button>`).join('') || '<div class="empty">Других чатов нет.</div>'}
      </div>
    </div>
    <div class="card">
      <h2>Что перенести</h2>
      <div class="wrap">${d.groups.map((g) => `
        <button class="chip on" data-act="copy-group" data-key="${g.key}"
                data-label="${esc(plain(g.label))}">✓ ${esc(plain(g.label))}</button>`).join('')}</div>
      <button class="btn wide" style="margin-top:12px" data-act="copy-run">📥 Перенести</button>
    </div>`,
  };
}

async function statsView(cid) {
  const d = await api(`/chat/${cid}/stats`);
  return {
    title: 'Статистика',
    back: `#/chat/${cid}`,
    html: `<div class="card">
      <div class="row"><div class="label">💬 Сообщений</div>
        <div class="value">сегодня ${d.d1} · 7д ${d.d7} · всего ${d.total}</div></div>
      <div class="row"><div class="label">👥 За 7 дней</div>
        <div class="value">пришло ${d.joins} · ушло ${d.leaves}</div></div>
      <div class="row"><div class="label">🔨 Наказаний за 7д</div><div class="value">${d.pun7}</div></div>
    </div>
    <div class="card">
      <h2>🏆 Топ за неделю</h2>
      ${d.top.map((t, i) => `<div class="item"><div class="body">${i + 1}. ${esc(t.who)}</div>
        <div class="value">${t.count}</div></div>`).join('') || '<div class="empty">Пока пусто.</div>'}
    </div>`,
  };
}

async function eventsView(cid) {
  const d = await api(`/chat/${cid}/events`);
  return {
    title: 'Лог чата',
    back: `#/chat/${cid}`,
    html: `<div class="card">
      ${d.items.map((e) => `<div class="item"><div class="body">${esc(e.text)}
        <small>${esc(e.when)}</small></div></div>`).join('') || '<div class="empty">Пока пусто.</div>'}
    </div>`,
  };
}

/* --- сетки --- */

/* Строки сеток; у владельца бота — с разбивкой по владельцам. */
function netRows(items) {
  const row = (n) => `<button class="item" style="width:100%;background:none;border:0;color:inherit;font:inherit;text-align:left"
      data-go="#/net/${n.id}">
      <div class="body">🕸 ${esc(n.title)}<small>${n.chats} ${num(n.chats, 'чат', 'чата', 'чатов')}</small></div>
      <div class="value">›</div></button>`;
  if (!INIT.owner) return items.map(row).join('');
  return groupByOwner(items).map((g) => `
    <div class="label" style="margin:10px 0 4px">👤 ${esc(g.owner)}</div>
    ${g.items.map(row).join('')}`).join('');
}

async function netsView() {
  const d = await api('/nets');
  return {
    title: 'Сетки чатов',
    back: '#/',
    html: `<div class="card">
      <div class="intro">Сетка — группа ваших чатов, между которыми разъезжаются наказания:
        бан в одном применяется во всех остальных. Чат состоит ровно в одной сетке или ни в одной.</div>
      <div style="margin-top:10px">
        ${netRows(d.items) || '<div class="empty">Пока ни одной сетки.</div>'}
      </div>
      ${d.can_create
        ? '<button class="btn wide" style="margin-top:10px" data-act="net-new">🆕 Создать сетку</button>'
        : `<div class="muted" style="margin-top:10px">Лимит: ${d.limit} сетки на человека.</div>`}
    </div>`,
  };
}

async function netView(nid) {
  const d = await api(`/net/${nid}`);
  return {
    title: d.title,
    back: '#/nets',
    html: `<div class="card">
      <h2>🕸 ${esc(d.title)}</h2>
      <div class="muted">Чатов в сетке: ${d.chats.length}</div>
      <div style="margin-top:8px">
        ${d.chats.map((c) => `<div class="item"><div class="body">${esc(c.title)}</div>
          <button class="x" data-act="net-rm" data-nid="${nid}" data-cid="${c.chat_id}">✕</button></div>`).join('')
          || '<div class="empty">Пока пусто.</div>'}
      </div>
      ${d.free.length ? `<button class="btn wide" style="margin-top:10px" data-act="net-add" data-nid="${nid}">➕ Добавить чат</button>` : ''}
    </div>
    <div class="card">
      <h2>Что синхронизировать</h2>
      ${chips(d.bits, 'net-bit')}
      <div class="row" style="margin-top:10px"><div class="label">🔓 Снимать может</div>
        <select data-net-lift="${nid}">
          <option value="any" ${d.lift_mode === 'any' ? 'selected' : ''}>любой чат</option>
          <option value="source" ${d.lift_mode === 'source' ? 'selected' : ''}>только тот, где выдали</option>
        </select></div>
    </div>
    <div class="card">
      ${d.chats.length > 1 ? `<button class="btn wide ghost" data-act="net-import" data-nid="${nid}">📥 Разослать активные баны по сетке</button>` : ''}
      <button class="btn wide ghost" style="margin-top:8px" data-act="net-rename" data-nid="${nid}">✏️ Переименовать</button>
      <button class="btn wide danger" style="margin-top:8px" data-act="net-del" data-nid="${nid}">🗑 Удалить сетку</button>
    </div>`,
  };
}

/* --- владельцу бота --- */

async function accessView() {
  const d = await api('/access');
  return {
    title: 'Доступ к боту',
    back: '#/',
    html: `<div class="card">
      <div class="intro">Кому разрешено настраивать бота и свои чаты.</div>
      <div style="margin-top:10px">
        ${d.items.map((r) => `<div class="item">
          <div class="body">${esc(r.who)}<small class="mono">${esc(r.user_id || ('@' + (r.username || '')))}</small></div>
          <button class="x" data-act="access-del" data-id="${r.id}">✕</button></div>`).join('')
          || '<div class="empty">Пусто — бот доступен только владельцу.</div>'}
      </div>
      <button class="btn wide" style="margin-top:10px" data-act="access-add">➕ Добавить</button>
    </div>`,
  };
}

// Стартовый набор общий на весь бот: удалили пример — он пропал у всех
// чатов сразу. Чужая «норма» из чата про Linux в чате про рыбалку только
// мешает, поэтому смысл страницы — быстро найти лишнее и выкинуть.
const SEED = { label: '', q: '', page: 0 };

async function seedView() {
  const p = new URLSearchParams({ label: SEED.label, q: SEED.q, page: SEED.page });
  const d = await api(`/seed?${p}`);
  CACHE.seed = d;
  const tab = (key, name) => `<button class="btn ${SEED.label === key ? '' : 'ghost'}"
    data-act="seed-label" data-label="${key}">${name}</button>`;
  return {
    title: 'Стартовый набор',
    back: '#/',
    html: `<div class="card">
      <div class="intro">Чужие примеры спама и обычных сообщений. Ими пользуется
        чат, пока не накопит своих ${d.until}; дальше набор отключается сам —
        своя норма всегда точнее чужой.<br><br>
        Набор общий: удалили пример здесь — он пропал у всех чатов сразу.</div>
      <div class="row"><div class="label">⛔ Спам</div>
        <div class="value">${d.stats.spam}</div></div>
      <div class="row"><div class="label">🕊 Норма</div>
        <div class="value">${d.stats.ok}</div></div>
      <div class="row"><div class="label">📦 Всего</div>
        <div class="value">${d.stats.total}</div></div>
      <div class="row"><div class="label">🧮 Посчитано векторов
        <small>в работе ${Math.min(d.in_work, d.stats.total)}, поровну того и другого</small></div>
        <div class="value">${d.vecs}</div></div>
    </div>

    <div class="card">
      <div class="row" style="gap:6px">
        ${tab('', 'Все')}${tab('spam', '⛔ Спам')}${tab('ok', '🕊 Норма')}
      </div>
      <button class="btn ghost wide" style="margin-top:10px" data-act="seed-search">
        🔎 ${SEED.q ? 'Поиск: ' + esc(SEED.q) : 'Найти по слову'}</button>
      ${SEED.q ? `<button class="btn ghost danger wide" style="margin-top:6px"
        data-act="seed-wipe">❌ Удалить всё найденное (${d.total})</button>
        <button class="btn ghost wide" style="margin-top:6px"
        data-act="seed-clearq">✖️ Сбросить поиск</button>` : ''}
    </div>

    <div class="card">
      <div class="label">Найдено: ${d.total}${d.pages > 1
        ? ` · страница ${d.page + 1} из ${d.pages}` : ''}</div>
      <div style="margin-top:10px">
        ${d.items.map((r) => `<div class="item">
          <div class="body">${r.label === 'spam' ? '⛔' : '🕊'}
            ${esc(r.text.slice(0, 300))}</div>
          <button class="x" data-act="seed-del" data-id="${r.id}">✕</button></div>`).join('')
          || '<div class="empty">Ничего не нашлось.</div>'}
      </div>
      ${d.pages > 1 ? `<div class="row" style="margin-top:10px;gap:6px">
        <button class="btn ghost" data-act="seed-page" data-page="${d.page - 1}"
          ${d.page ? '' : 'disabled'}>⬅️</button>
        <button class="btn ghost" data-act="seed-page" data-page="${d.page + 1}"
          ${d.page + 1 < d.pages ? '' : 'disabled'}>➡️</button>
      </div>` : ''}
    </div>

    <div class="card">
      <button class="btn ghost danger wide" data-act="seed-clear">
        🧹 Очистить набор целиком</button>
      <div class="intro" style="margin-top:8px">Загрузить заново можно только
        с машины: <span class="mono">python tools/import_dataset.py файл</span></div>
    </div>`,
  };
}

async function rouletteView() {
  const d = await api('/fun/roulette');
  const c = d.cfg;
  CACHE.roulette = d;
  return {
    title: 'Бан-рулетка',
    back: '#/',
    html: `<div class="card">
      <div class="intro">Бот объявляет розыгрыш, крутит барабан и выдаёт наказание победителю.
        «Весь чат» — участвуют все, кто писал за месяц. «По кнопке» — только нажавшие.
        Админы не участвуют.</div>
      <div class="row"><div class="label">💬 Чат</div>
        <select data-rl="chat_id">
          <option value="0">не выбран</option>
          ${d.chats.map((x) => `<option value="${x.chat_id}" ${x.chat_id === c.chat_id ? 'selected' : ''}>${esc(x.title)}</option>`).join('')}
        </select></div>
      <div class="row"><div class="label">🔨 Приз</div>
        <select data-rl="kind">
          <option value="mute" ${c.kind === 'mute' ? 'selected' : ''}>мут</option>
          <option value="ban" ${c.kind === 'ban' ? 'selected' : ''}>бан</option>
        </select></div>
      ${c.kind === 'mute' ? `<div class="row"><div class="label">⏰ Срок</div>
        <select data-rl="minutes">
          ${d.mutes.map((m) => `<option value="${m.value}" ${m.value === c.minutes ? 'selected' : ''}>${esc(m.label)}</option>`).join('')}
        </select></div>` : ''}
      <div class="row"><div class="label">🎛 Режим</div>
        <select data-rl="mode">
          <option value="all" ${c.mode === 'all' ? 'selected' : ''}>весь чат</option>
          <option value="opt" ${c.mode === 'opt' ? 'selected' : ''}>по кнопке</option>
        </select></div>
      ${c.mode === 'opt' ? `<div class="row"><div class="label">⏳ Сбор, сек</div>
        <select data-rl="timer">
          ${d.timers.map((t) => `<option value="${t}" ${t === c.timer ? 'selected' : ''}>${t}</option>`).join('')}
        </select></div>` : ''}
      ${c.chat_id ? '<button class="btn wide danger" style="margin-top:12px" data-act="rl-spin">🎲 Крутить!</button>' : ''}
    </div>`,
  };
}

async function adminLogView() {
  const d = await api('/admin/log');
  return {
    title: 'Лог событий',
    back: '#/',
    html: `<div class="card">${d.items.map((e) => `<div class="item">
      <div class="body">${esc(e.text)}<small>${esc(e.when)}${e.chat ? ' · ' + esc(e.chat) : ''}</small></div>
    </div>`).join('') || '<div class="empty">Пока пусто.</div>'}</div>`,
  };
}

async function adminErrorsView() {
  const d = await api('/admin/errors');
  return {
    title: 'Ошибки',
    back: '#/',
    html: `<div class="card">${d.items.length
      ? `<pre class="log">${esc(d.items.join('\n\n'))}</pre>`
      : '<div class="empty">Ошибок нет.</div>'}</div>`,
  };
}

async function adminHealthView() {
  const d = await api('/admin/health');
  return {
    title: 'Состояние',
    back: '#/',
    html: `<div class="card"><pre class="log">${esc(d.text)}</pre></div>`,
  };
}

/* ---------- маршруты ---------- */

const ROUTES = [
  [/^$/, homeView],
  [/^help$/, helpView],
  [/^nets$/, netsView],
  [/^net\/(\d+)$/, netView],
  [/^access$/, accessView],
  [/^seed$/, seedView],
  [/^roulette$/, rouletteView],
  [/^admin\/log$/, adminLogView],
  [/^admin\/errors$/, adminErrorsView],
  [/^admin\/health$/, adminHealthView],
  [/^chat\/(-?\d+)$/, chatView],
  [/^chat\/(-?\d+)\/s\/(\w+)$/, sectionView],
  [/^chat\/(-?\d+)\/words$/, wordsView],
  [/^chat\/(-?\d+)\/wl\/(\d+)$/, wlEntryView],
  [/^chat\/(-?\d+)\/linkwl$/, linkwlView],
  [/^chat\/(-?\d+)\/trigs$/, trigsView],
  [/^chat\/(-?\d+)\/trig\/(\d+)$/, trigView],
  [/^chat\/(-?\d+)\/cmds$/, cmdsView],
  [/^chat\/(-?\d+)\/cmd\/(\d+)$/, cmdView],
  [/^chat\/(-?\d+)\/answers\/(\w+)\/(-?\d+)$/, answersView],
  [/^chat\/(-?\d+)\/warned$/, warnedView],
  [/^chat\/(-?\d+)\/active$/, activeView],
  [/^chat\/(-?\d+)\/games$/, gamesView],
  [/^chat\/(-?\d+)\/copy$/, copyView],
  [/^chat\/(-?\d+)\/stats$/, statsView],
  [/^chat\/(-?\d+)\/events$/, eventsView],
];

function here() {
  return decodeURIComponent(location.hash.replace(/^#\/?/, ''));
}

/* Текущий чат — нужен обработчикам действий, чтобы не тащить его параметром. */
function curChat() {
  const m = here().match(/^chat\/(-?\d+)/);
  return m ? m[1] : null;
}

async function render() {
  const path = here();
  for (const [re, view] of ROUTES) {
    const m = path.match(re);
    if (!m) continue;
    try {
      const page = await view(...m.slice(1));
      $app.innerHTML = page.html;
      $title.textContent = page.title;
      const back = page.back || (path ? '#/' : null);
      $back.hidden = !back;
      $back.dataset.go = back || '';
      if (tg && tg.BackButton) {
        if (back) tg.BackButton.show(); else tg.BackButton.hide();
      }
      window.scrollTo(0, 0);
    } catch (e) {
      $app.innerHTML = `<div class="card"><div class="empty">${esc(e.message)}</div></div>`;
    }
    return;
  }
  location.hash = '#/';
}

const go = (hash) => { location.hash = hash; };

/* ---------- действия ---------- */

const ACT = {
  /* --- главная и служебное --- */
  async 'global-log'() {
    const v = await ask({ title: 'Глобальный лог', value: (INIT.global_log && INIT.global_log.chat_id) || '',
      hint: 'Числовой id чата, куда копией летят все карточки. Дефис — убрать.' });
    if (v === null) return;
    const r = await api('/global-log', { json: { chat_id: v } });
    INIT.global_log = r;
    toast(r.chat_id ? 'Глобальный лог обновлён' : 'Глобальный лог убран');
    render();
  },

  /* --- карточка чата --- */
  async 'setup-skip'() {
    await api(`/chat/${curChat()}/setup-skip`, { json: {} });
    render();
  },

  async 'set-log'() {
    const cid = curChat();
    const v = await ask({ title: 'Лог-чат', hint: 'Числовой id чата для карточек. Дефис — убрать лог-чат.',
      value: (CACHE.chat && CACHE.chat.log_chat.chat_id) || '' });
    if (v === null) return;
    const r = await api(`/chat/${cid}/log`, { json: { chat_id: v } });
    toast(r.chat_id ? 'Лог-чат обновлён' : 'Лог-чат убран');
    render();
  },

  async 'chat-net'() {
    const cid = curChat();
    const nets = await api('/nets');
    const options = [{ value: '0', label: 'без сетки' }]
      .concat(nets.items.map((n) => ({ value: String(n.id), label: n.title })));
    const cur = CACHE.chat && CACHE.chat.net ? String(CACHE.chat.net.id) : '0';
    const v = await pick({ title: 'Сетка чата', options, value: cur });
    if (v === null) return;
    await api(`/chat/${cid}/net`, { json: { net_id: v } });
    toast('Готово');
    render();
  },

  async leave() {
    if (!await confirmAsk('Точно убрать бота из чата?')) return;
    await api(`/chat/${curChat()}/leave`, { json: {} });
    toast('Бот вышел из чата');
    INIT = await api('/init');
    go('#/');
  },

  /* --- списки --- */
  async 'words-add'() {
    const v = await ask({ title: 'Стоп-слова', multiline: true,
      hint: 'Через запятую или с новой строки. <code>слово</code> — точно, <code>слово*</code> — с окончаниями.' });
    if (!v) return;
    const r = await api(`/chat/${curChat()}/words`, { json: { text: v } });
    toast(`Добавлено: ${r.added}${r.dupes ? ', уже были: ' + r.dupes : ''}`);
    render();
  },

  async 'word-del'(el) {
    await api(`/chat/${curChat()}/words/${el.dataset.id}`, { method: 'DELETE' });
    render();
  },

  async 'words-clear'() {
    if (!await confirmAsk('Удалить все стоп-слова?')) return;
    const r = await api(`/chat/${curChat()}/words/clear`, { json: {} });
    toast(`Удалено: ${r.removed}`);
    render();
  },

  async 'phrase-add'() {
    const v = await ask({ title: 'Фраза-образец',
      hint: 'Так, как пишут спамеры. Несколько — каждая с новой строки.' });
    if (!v) return;
    const r = await api(`/chat/${curChat()}/phrases`, { json: { text: v } });
    toast(`Добавлено: ${r.added}${r.dupes ? ', уже были: ' + r.dupes : ''}`);
    render();
  },

  async 'phrase-del'(el) {
    await api(`/chat/${curChat()}/phrases/${el.dataset.id}`, { method: 'DELETE' });
    render();
  },

  async 'nn-doubt'() {
    const box = document.getElementById('clusters');
    markTab('doubt');
    box.innerHTML = '<div class="muted" style="margin-top:12px">Считаю…</div>';
    const r = await api(`/chat/${curChat()}/nn/doubt`);
    if (!r.items.length) {
      box.innerHTML = `<div class="muted" style="margin-top:12px">${
        r.model === 'ok' ? 'Спорного нет — либо копилка пуста, либо всё однозначно.'
                         : 'Модель не загружена: ' + esc(r.model)}</div>`;
      return;
    }
    box.innerHTML = r.items.map((it) => `
      <div class="card" style="margin-top:10px">
        <div class="label"><b>оценка ${it.score}%</b></div>
        <div class="muted" style="margin:6px 0">${esc(it.text.slice(0, 200))}</div>
        <div class="row">
          <button class="btn" data-act="doubt-mark" data-id="${it.id}"
            data-label="spam">⛔ Спам</button>
          <button class="btn ghost" data-act="doubt-mark" data-id="${it.id}"
            data-label="ok">🕊 Норма</button>
        </div>
      </div>`).join('');
  },

  async 'doubt-mark'(el) {
    await api(`/chat/${curChat()}/nn/doubt`,
              { json: { id: Number(el.dataset.id), label: el.dataset.label } });
    toast('Размечено');
    await ACT['nn-doubt']();
  },

  async 'nn-clusters'(el) {
    const box = document.getElementById('clusters');
    const scope = el.dataset.scope;
    markTab(scope);
    box.innerHTML = '<div class="muted" style="margin-top:12px">Считаю…</div>';
    const r = await api(`/chat/${curChat()}/nn/clusters?scope=${scope}`);
    if (!r.items.length) {
      box.innerHTML = `<div class="muted" style="margin-top:12px">${
        r.model === 'ok' ? `Улик пока мало — нужно хотя бы ${r.min}.`
                         : 'Модель не загружена: ' + esc(r.model)}</div>`;
      return;
    }
    box.innerHTML = r.items.map((g, i) => `
      <div class="card" style="margin-top:10px">
        <div class="label"><b>${i + 1}. ${g.size} шт</b>
          <small>${esc(g.words.join(', ') || '—')}</small></div>
        <div class="muted" style="margin:6px 0">${esc(g.sample.slice(0, 160))}</div>
        <div class="muted" style="margin:6px 0">${clusterState(g)}</div>
        <div class="row">
          <button class="btn ghost danger" data-act="nn-label" data-i="${i}"
            data-label="spam" data-scope="${scope}">⛔ Пометить спамом</button>
          <button class="btn ghost good" data-act="nn-label" data-i="${i}"
            data-label="ok" data-scope="${scope}">🕊 Пометить нормой</button>
        </div>
      </div>`).join('');
  },

  async 'nn-label'(el) {
    const r = await api(`/chat/${curChat()}/nn/clusters`,
                        { json: { index: Number(el.dataset.i), label: el.dataset.label } });
    toast(r.moved ? `Размечено: ${r.moved}` : 'Разбивка устарела, пересчитайте');
    await ACT['nn-clusters']({ dataset: { scope: el.dataset.scope } });
  },

  async 'wl-add'() {
    const v = await ask({ title: 'Вайтлист', hint: 'id или @username — человека либо канала.' });
    if (!v) return;
    const r = await api(`/chat/${curChat()}/wl`, { json: { target: v } });
    toast(r.note);
    go(`#/chat/${curChat()}/wl/${r.row_id}`);
  },

  async 'wl-scope'(el) {
    await api(`/chat/${curChat()}/wl/${el.dataset.row}/scope`, { json: { scope: el.dataset.scope } });
    render();
  },

  async 'wl-del'(el) {
    if (!await confirmAsk('Убрать из вайтлиста?')) return;
    await api(`/chat/${curChat()}/wl/${el.dataset.row}`, { method: 'DELETE' });
    go(`#/chat/${curChat()}/s/wl`);
  },

  async 'linkwl-add'() {
    const v = await ask({ title: 'Разрешить чат или канал', hint: '@username или id.' });
    if (!v) return;
    toast((await api(`/chat/${curChat()}/linkwl`, { json: { target: v } })).note);
    render();
  },

  async 'linkwl-del'(el) {
    await api(`/chat/${curChat()}/linkwl/${el.dataset.id}`, { method: 'DELETE' });
    render();
  },

  async 'inlinewl-add'() {
    const v = await ask({ title: 'Разрешённый инлайн-бот', hint: '@username бота, например @gif.' });
    if (!v) return;
    toast((await api(`/chat/${curChat()}/inlinewl`, { json: { target: v } })).note);
    render();
  },

  async 'inlinewl-del'(el) {
    await api(`/chat/${curChat()}/inlinewl/${el.dataset.id}`, { method: 'DELETE' });
    render();
  },

  /* --- триггеры и счётчики --- */
  async 'trig-add'() {
    const phrase = await ask({ title: 'Новый триггер', ok: 'Дальше',
      hint: 'Ключевая фраза от 3 символов. Срабатывает целиком: <code>донат</code> не поймает «донатный», нужна звёздочка — <code>донат*</code>.' });
    if (!phrase) return;
    const text = await ask({ title: 'Ответ бота', multiline: true,
      hint: 'Текст ответа. Медиа добавите потом кнопкой «Загрузить медиа».' });
    if (text === null) return;
    const r = await api(`/chat/${curChat()}/trigs`, { json: { phrase, text } });
    toast(r.note);
    go(`#/chat/${curChat()}/trig/${r.id}`);
  },

  async 'trig-phrase'(el) {
    const v = await ask({ title: 'Новая фраза' });
    if (!v) return;
    await api(`/chat/${curChat()}/trigs/${el.dataset.id}`, { json: { phrase: v } });
    render();
  },

  async 'trig-del'(el) {
    if (!await confirmAsk('Удалить триггер вместе с ответами?')) return;
    await api(`/chat/${curChat()}/trigs/${el.dataset.id}`, { method: 'DELETE' });
    go(`#/chat/${curChat()}/trigs`);
  },

  async 'cmd-add'() {
    const cmd = await ask({ title: 'Новый счётчик', ok: 'Дальше',
      hint: 'Команда одним словом, например <code>!кузнечик</code>.' });
    if (!cmd) return;
    const text = await ask({ title: 'Заготовка ответа',
      hint: 'Бот допишет счёт: «кузнечики [1]», «кузнечики [2]»…' });
    if (!text) return;
    const r = await api(`/chat/${curChat()}/cmds`, { json: { cmd, text } });
    toast(r.note);
    go(`#/chat/${curChat()}/cmd/${r.id}`);
  },

  async 'cmd-reset'(el) {
    await api(`/chat/${curChat()}/cmds/${el.dataset.id}`, { json: { reset: true } });
    toast('Счёт сброшен');
    render();
  },

  async 'cmd-del'(el) {
    if (!await confirmAsk('Удалить счётчик?')) return;
    await api(`/chat/${curChat()}/cmds/${el.dataset.id}`, { method: 'DELETE' });
    go(`#/chat/${curChat()}/cmds`);
  },

  async 'ans-add'(el) {
    const v = await ask({ title: 'Новый вариант', multiline: true });
    if (!v) return;
    await api(`/chat/${curChat()}/answers`, { json: { owner: el.dataset.owner, oid: +el.dataset.oid, text: v } });
    render();
  },

  async 'ans-del'(el) {
    await api(`/chat/${curChat()}/answers/${el.dataset.id}`, { method: 'DELETE' });
    render();
  },

  async 'welcome-migrate'() {
    await api(`/chat/${curChat()}/welcome/migrate`, { json: {} });
    toast('Перенесено');
    render();
  },

  /* --- варны и наказания --- */
  async 'warn-reset'(el) {
    await api(`/chat/${curChat()}/warned/${el.dataset.uid}/reset`, { json: {} });
    toast('Варны сняты');
    render();
  },

  async lift(el) {
    const r = await api(`/chat/${curChat()}/active/${el.dataset.id}/lift`, { json: {} });
    toast(r.note || (r.ok ? 'Снято' : 'Не вышло'));
    render();
  },

  async mass(el) {
    const kind = el.dataset.kind;
    const titles = { ban: 'Массовый бан', unban: 'Массовый разбан', kick: 'Массовый кик' };
    const v = await ask({ title: titles[kind], multiline: true,
      hint: 'Список id или @username через пробел, запятую или с новой строки.' });
    if (!v) return;
    toast('Работаю, это займёт время…');
    const r = await api(`/chat/${curChat()}/mass`, { json: { kind, text: v } });
    const parts = [];
    if (r.done.length) parts.push(`✅ ${r.done.length}`);
    if (r.skip.length) parts.push(`➖ ${r.skip.length}`);
    if (r.fail.length) parts.push(`⚠️ ${r.fail.length}`);
    toast(parts.join(' · ') || 'Ничего не изменилось');
    render();
  },

  /* --- игры --- */
  async 'game-who'(el) {
    await api(`/chat/${curChat()}/bit`, { json: { key: 'games_adm', bit: +el.dataset.bit } });
    render();
  },

  async 'game-kind'(el) {
    const v = await pick({ title: 'Приз проигравшему',
      options: [{ value: 'mute', label: 'мут' }, { value: 'ban', label: 'бан' }] });
    if (!v) return;
    await api(`/chat/${curChat()}/games/prize`, { json: { bit: +el.dataset.bit, kind: v } });
    render();
  },

  async 'game-min'(el) {
    const d = await api(`/chat/${curChat()}/games`);
    const v = await pick({ title: 'Срок мута', options: d.mutes });
    if (v === null) return;
    await api(`/chat/${curChat()}/games/prize`, { json: { bit: +el.dataset.bit, minutes: +v } });
    render();
  },

  /* --- перенос настроек --- */
  'copy-src'(el) {
    CACHE.copy.src = el.dataset.src;
    document.querySelectorAll('[data-src-mark]').forEach((m) => {
      m.textContent = m.dataset.srcMark === el.dataset.src ? '✓' : '○';
    });
  },

  'copy-group'(el) {
    const set = CACHE.copy.groups;
    const key = el.dataset.key;
    const on = !set.has(key);
    if (on) set.add(key); else set.delete(key);
    el.classList.toggle('on', on);
    el.textContent = (on ? '✓ ' : '○ ') + el.dataset.label;
  },

  async 'copy-run'() {
    const { src, groups } = CACHE.copy;
    if (!src) return toast('Сначала выберите чат-источник');
    if (!groups.size) return toast('Не выбрано ни одного раздела');
    if (!await confirmAsk('Перенести выбранные настройки сюда?')) return;
    const r = await api(`/chat/${curChat()}/copy`, { json: { src: +src, groups: [...groups] } });
    toast(Object.entries(r.copied).filter(([, n]) => n).map(([k, n]) => `${k}: ${n}`).join(' · ') || 'Готово');
    go(`#/chat/${curChat()}`);
  },

  /* --- сетки --- */
  async 'net-new'() {
    const v = await ask({ title: 'Новая сетка', hint: 'Название, от 2 символов.' });
    if (!v) return;
    const r = await api('/nets', { json: { title: v } });
    INIT = await api('/init');
    go(`#/net/${r.id}`);
  },

  async 'net-rename'(el) {
    const v = await ask({ title: 'Новое название' });
    if (!v) return;
    await api(`/net/${el.dataset.nid}`, { json: { title: v } });
    render();
  },

  async 'net-del'(el) {
    if (!await confirmAsk('Удалить сетку? Чаты останутся, связь между ними пропадёт.')) return;
    await api(`/net/${el.dataset.nid}`, { method: 'DELETE' });
    INIT = await api('/init');
    go('#/nets');
  },

  async 'net-bit'(el) {
    const nid = here().match(/^net\/(\d+)/)[1];
    await api(`/net/${nid}`, { json: { bit: +el.dataset.bit } });
    render();
  },

  async 'net-add'(el) {
    const nid = el.dataset.nid;
    const d = await api(`/net/${nid}`);
    const v = await pick({ title: 'Какой чат добавить',
      options: d.free.map((c) => ({ value: String(c.chat_id), label: c.title + (c.busy ? ` (сейчас в «${c.busy}»)` : '') })) });
    if (!v) return;
    await api(`/net/${nid}/chats`, { json: { chat_id: +v } });
    render();
  },

  async 'net-rm'(el) {
    await api(`/net/${el.dataset.nid}/chats/${el.dataset.cid}`, { method: 'DELETE' });
    render();
  },

  async 'net-import'(el) {
    if (!await confirmAsk('Разослать активные баны по всем чатам сетки?')) return;
    toast('Свожу баны сетки, это займёт время…');
    const r = await api(`/net/${el.dataset.nid}/import`, { json: {} });
    toast(`Заведено банов: ${r.done}${r.failed ? ', не вышло: ' + r.failed : ''}`);
  },

  /* --- доступ --- */
  async 'access-add'() {
    const v = await ask({ title: 'Доступ к боту', hint: 'id или @username.' });
    if (!v) return;
    await api('/access', { json: { target: v } });
    render();
  },

  async 'seed-label'(el) {
    SEED.label = el.dataset.label;
    SEED.page = 0;
    await render();
  },

  async 'seed-page'(el) {
    SEED.page = Number(el.dataset.page);
    await render();
  },

  async 'seed-search'() {
    const v = await ask({ title: 'Поиск в наборе',
                          hint: 'Слово или кусок фразы — например docker, ядро, systemd.' });
    if (v === null) return;
    SEED.q = v.trim();
    SEED.page = 0;
    await render();
  },

  async 'seed-clearq'() {
    SEED.q = '';
    SEED.page = 0;
    await render();
  },

  async 'seed-del'(el) {
    const r = await api('/seed/delete', { json: { ids: [Number(el.dataset.id)] } });
    toast(`Удалено: ${r.gone}`);
    await render();
  },

  async 'seed-wipe'() {
    const n = (CACHE.seed && CACHE.seed.total) || 0;
    if (!await confirmAsk(`Удалить ${n} ${num(n, 'пример', 'примера', 'примеров')} `
                       + `по «${SEED.q}»? Они пропадут у всех чатов.`)) return;
    const r = await api('/seed/delete', { json: { label: SEED.label, q: SEED.q } });
    toast(`Удалено: ${r.gone}`);
    SEED.q = '';
    SEED.page = 0;
    await render();
  },

  async 'seed-clear'() {
    const n = (CACHE.seed && CACHE.seed.stats.total) || 0;
    if (!await confirmAsk(`Удалить весь набор — все ${n}? Молодые чаты снова `
                       + 'останутся без образцов.')) return;
    const r = await api('/seed/delete', { json: { all: true } });
    toast(`Удалено: ${r.gone}`);
    SEED.q = '';
    SEED.page = 0;
    await render();
  },

  async 'access-del'(el) {
    await api(`/access/${el.dataset.id}`, { method: 'DELETE' });
    render();
  },

  /* --- сводка --- */
  async 'digest-to'() {
    const v = await ask({ title: 'Получатель сводки', hint: 'Числовой id человека.' });
    if (!v) return;
    await api(`/chat/${curChat()}/digest`, { json: { to: v } });
    render();
  },

  async 'digest-off'() {
    await api(`/chat/${curChat()}/digest`, { json: { off: true } });
    render();
  },

  async 'digest-now'() {
    toast((await api(`/chat/${curChat()}/digest`, { json: { now: true } })).note);
  },

  /* --- рулетка --- */
  async 'rl-spin'() {
    if (!await confirmAsk('Запустить рулетку в выбранном чате?')) return;
    toast((await api('/fun/roulette/spin', { json: {} })).note);
  },

  /* --- биты масок --- */
  async 'bit-card'(el) { await bitToggle('card_mask', el); },
  async 'bit-media'(el) { await bitToggle('media_mask', el); },
  async 'bit-trust'(el) { await bitToggle('trust_mask', el); },
};

async function bitToggle(key, el) {
  await api(`/chat/${curChat()}/bit`, { json: { key, bit: +el.dataset.bit } });
  haptic();
  render();
}

/* ---------- слушатели ---------- */

document.addEventListener('click', async (e) => {
  const goEl = e.target.closest('[data-go]');
  if (goEl && goEl.dataset.go) { go(goEl.dataset.go); return; }

  const actEl = e.target.closest('[data-act]');
  if (!actEl) return;
  const fn = ACT[actEl.dataset.act];
  if (!fn) return;
  e.preventDefault();
  try {
    await fn(actEl);
  } catch (err) {
    toast(err.message);
  }
});

document.addEventListener('change', async (e) => {
  const el = e.target;
  try {
    if (el.dataset.toggle !== undefined) {
      await api(`/chat/${curChat()}/set`, { json: { key: el.dataset.toggle, value: el.checked ? 1 : 0 } });
      haptic();
      render();
    } else if (el.dataset.select !== undefined) {
      await api(`/chat/${curChat()}/set`, { json: { key: el.dataset.select, value: el.value } });
      render();
    } else if (el.dataset.game !== undefined) {
      await api(`/chat/${curChat()}/bit`, { json: { key: 'games_on', bit: +el.dataset.game } });
      render();
    } else if (el.dataset.cooldown) {
      const path = el.dataset.cooldown === 'trig' ? 'trigs' : 'cmds';
      await api(`/chat/${curChat()}/${path}/${el.dataset.id}`, { json: { cooldown: +el.value } });
      toast('Сохранено');
    } else if (el.dataset.netLift) {
      await api(`/net/${el.dataset.netLift}`, { json: { lift_mode: el.value } });
      toast('Сохранено');
    } else if (el.dataset.rl) {
      const body = {};
      body[el.dataset.rl] = el.dataset.rl === 'kind' || el.dataset.rl === 'mode' ? el.value : +el.value;
      await api('/fun/roulette', { json: body });
      render();
    } else if (el.dataset.upload === 'answer') {
      const caption = await ask({ title: 'Подпись к медиа', hint: 'Можно оставить пустой.', ok: 'Загрузить' });
      if (caption === null) { el.value = ''; return; }
      const fd = new FormData();
      fd.append('owner', el.dataset.owner);
      fd.append('oid', el.dataset.oid);
      fd.append('caption', caption);
      fd.append('file', el.files[0]);
      await api(`/chat/${curChat()}/answers/upload`, { form: fd });
      toast('Медиа добавлено');
      render();
    }
  } catch (err) {
    toast(err.message);
    render();
  }
});

$back.addEventListener('click', () => { if ($back.dataset.go) go($back.dataset.go); });
if (tg && tg.BackButton) tg.BackButton.onClick(() => { if ($back.dataset.go) go($back.dataset.go); });
window.addEventListener('hashchange', render);

/* ---------- старт ---------- */

(async function boot() {
  try {
    INIT = await api('/init');
  } catch (e) {
    $app.innerHTML = `<div class="card"><div class="empty">${esc(e.message)}</div></div>`;
    return;
  }
  if (!location.hash) location.hash = '#/';
  render();
})();
