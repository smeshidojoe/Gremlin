/* Панель Gremlin: то же меню бота, но страницей.
 *
 * Устройство простое и намеренно без сборки: маршрут лежит в hash, каждая
 * страница — функция, которая сходила в API и вернула html. Обработчики не
 * навешиваем поштучно: один слушатель на документ разбирает data-act.
 */
'use strict';

const tg = window.Telegram && window.Telegram.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  // свайп вниз закрывал панель прямо посреди прокрутки длинного списка
  try { tg.disableVerticalSwipes(); } catch (e) { /* старые клиенты не умеют */ }
}

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
/* Пояснение: пустая строка — новый абзац, перенос — новая строка. Без этого
   HTML схлопывал переносы, и пояснение читалось сплошным полотном. */
const introHtml = (text) => String(text || '').split(/\n{2,}/)
  .map((p) => `<p>${p.replace(/\n/g, '<br>')}</p>`).join('');

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

/* Ответы на чтение держим в памяти: вернулся на страницу — она появляется
   сразу, а свежие данные подтягиваются в фоне. Любое изменение чистит кэш
   целиком: это дешевле, чем гадать, какие страницы задел ответ сервера. */
const GET_CACHE = new Map();
const FRESH_MS = 3000;     // моложе — за свежим в фон не ходим

async function fetchJson(path, init) {
  const r = await fetch('/api' + path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ('Ошибка ' + r.status));
  return data;
}

/* Фоновое обновление: без индикатора и без ошибок наружу — на экране уже
   есть прошлый ответ. Пришло другое и страница та же — перерисовываем. */
async function refresh(path, headers) {
  if (refresh.busy.has(path)) return;
  refresh.busy.add(path);
  const was = here();
  try {
    const data = await fetchJson(path, { method: 'GET', headers });
    const old = GET_CACHE.get(path);
    GET_CACHE.set(path, { data, at: Date.now() });
    if (here() === was && JSON.stringify(old && old.data) !== JSON.stringify(data)) render();
  } catch (e) {
    /* молчим: страница уже показана */
  } finally {
    refresh.busy.delete(path);
  }
}
refresh.busy = new Set();

async function api(path, opts = {}) {
  const headers = { 'X-Init-Data': (tg && tg.initData) || '' };
  let body;
  if (opts.form) {
    body = opts.form;                       // FormData сам проставит границы
  } else if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.json);
  }
  const method = opts.method || (body ? 'POST' : 'GET');
  if (method !== 'GET') GET_CACHE.clear();
  const hit = method === 'GET' ? GET_CACHE.get(path) : null;
  if (hit) {
    if (Date.now() - hit.at > FRESH_MS) refresh(path, headers);
    return hit.data;
  }
  spin(true);
  try {
    const data = await fetchJson(path, { method, headers, body });
    if (method === 'GET') GET_CACHE.set(path, { data, at: Date.now() });
    else hapticDone(true);       // система ответила на действие
    return data;
  } finally {
    spin(false);
  }
}

function toast(text) {
  const el = document.getElementById('toast');
  el.textContent = text;
  el.hidden = false;
  // класс вешаем следующим кадром: иначе браузеру нечего анимировать —
  // элемент и появился, и оказался на месте в одном кадре
  requestAnimationFrame(() => el.classList.add('show'));
  clearTimeout(toast._t);
  toast._t = setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => { if (!el.classList.contains('show')) el.hidden = true; }, 200);
  }, 2600);
}

function haptic(kind) {
  try { tg.HapticFeedback.impactOccurred(kind || 'light'); } catch (e) { /* не всякий клиент умеет */ }
}

/* Переключили значение — щелчок выбора, а не удар. */
function hapticPick() {
  try { tg.HapticFeedback.selectionChanged(); } catch (e) { /* не всякий клиент умеет */ }
}

/* Действие закончилось: успех или ошибка. */
function hapticDone(ok) {
  try { tg.HapticFeedback.notificationOccurred(ok ? 'success' : 'error'); } catch (e) { /* … */ }
}

function confirmAsk(text) {
  return new Promise((resolve) => {
    if (tg && tg.showConfirm) tg.showConfirm(text, resolve);
    else resolve(window.confirm(text));
  });
}

/* Нижняя шторка: выезжает снизу, уходит тем же путём и закрывается свайпом
   вниз — как ведут себя шторки в самом Telegram. Возвращает функцию
   закрытия: ею же пользуются кнопки внутри. */
function sheetOpen(box, resolve) {
  box.hidden = false;
  requestAnimationFrame(() => box.classList.add('open'));
  const sheet = box.querySelector('.sheet');
  const close = (val) => {
    box.classList.remove('open');
    setTimeout(() => { box.hidden = true; box.innerHTML = ''; }, 220);
    resolve(val);
  };
  dragToClose(sheet, () => close(null));
  return close;
}

function dragToClose(sheet, close) {
  let from = null, moved = 0, started = 0;
  sheet.addEventListener('pointerdown', (e) => {
    // за поле ввода и кнопки не тянем: там свои жесты
    if (e.target.closest('input, textarea, select, button')) return;
    from = e.clientY;
    started = Date.now();
    moved = 0;
    sheet.setPointerCapture(e.pointerId);
    sheet.style.transition = 'none';
  });
  sheet.addEventListener('pointermove', (e) => {
    if (from === null) return;
    moved = Math.max(0, e.clientY - from);      // вверх шторка не едет
    sheet.style.transform = `translateY(${moved}px)`;
  });
  const release = () => {
    if (from === null) return;
    from = null;
    sheet.style.transition = '';
    // быстрый смах закрывает, даже если утянули недалеко
    const speed = moved / Math.max(1, Date.now() - started);
    if (moved > sheet.offsetHeight * 0.3 || speed > 0.11) close();
    else sheet.style.transform = '';
  };
  sheet.addEventListener('pointerup', release);
  sheet.addEventListener('pointercancel', release);
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
    const close = sheetOpen(box, resolve);
    const input = document.getElementById('ask-input');
    input.focus();
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
    const close = sheetOpen(box, resolve);
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
  if (g.spam) parts.push(`⛔ спамом — ${g.spam} (${Math.round(g.spam / g.size * 100)}%)`);
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
    // key — по нему запоминаем, какие группы человек свернул
    .map(([k, g]) => ({ ...g, key: String(k) }));
}

/* ---------- свёрнутые группы владельцев ----------
   У владельца бота в списке чаты нескольких человек, и обычно нужен один.
   Что свёрнуто, помним между заходами: иначе каждый раз сворачивай заново.
   Хранилище может быть недоступно (приватное окно, запрет на данные сайтов),
   поэтому любое обращение к нему — под try. */

const FOLD_KEY = 'gremlin.folded';
let FOLDED = new Set();
try {
  FOLDED = new Set(JSON.parse(localStorage.getItem(FOLD_KEY) || '[]'));
} catch (e) { /* не запомнили — не беда, покажем всё развёрнутым */ }

function saveFolded() {
  try {
    localStorage.setItem(FOLD_KEY, JSON.stringify([...FOLDED]));
  } catch (e) { /* некуда сохранить — свёрнутое живёт до перезахода */ }
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
  const groups = d.owner ? groupByOwner(d.chats) : [];
  const allFolded = groups.length > 0 && groups.every((g) => FOLDED.has(g.key));
  const chats = d.owner ? `
    ${groups.length > 1 ? `<button class="btn ghost small" style="margin:4px 0"
      data-act="fold-all">${allFolded ? '▼ Развернуть все' : '▶ Свернуть все'}</button>` : ''}
    ${groups.map((g) => {
      const off = FOLDED.has(g.key);
      return `<button class="fold" data-act="fold" data-key="${esc(g.key)}">
          <span>${off ? '▶' : '▼'} 👤 ${esc(g.owner)}</span>
          <span class="muted">${g.items.length} ${num(g.items.length, 'чат', 'чата', 'чатов')}</span>
        </button>
        ${off ? '' : `<div class="tiles chat-list">${g.items.map(tileFor).join('')}</div>`}`;
    }).join('')}`
    : `<div class="tiles chat-list">${d.chats.map(tileFor).join('')}</div>`;

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
      <div class="intro">Сначала лог-чат: туда бот пишет, кого наказал и за что, кто
        просится в чат и на кого пожаловались, и ставит кнопки — снять наказание,
        забанить, впустить. Без него бот работает молча. Заведите под это отдельную
        группу и добавьте бота туда администратором.
        ${d.log_chat.chat_id ? `Сейчас: <b>${esc(d.log_chat.title)}</b>.` : 'Сейчас не выбран.'}</div>
      <div class="intro" style="margin-top:6px">Правила можно не настраивать заново —
        перенесите из другого своего чата: фильтры, стоп-слова, вайтлисты, триггеры и
        счётчики целиком, вместе с медиа.</div>
      <div class="wrap" style="margin-top:10px">
        <button class="btn" data-act="set-log">📍 ${d.log_chat.chat_id ? 'Сменить лог-чат' : 'Выбрать лог-чат'}</button>
        <button class="btn ghost" data-go="#/chat/${cid}/copy">📥 Перенести настройки</button>
        <button class="btn ghost" data-act="setup-skip">🛠 Дальше сам</button>
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
        ${d.bot ? `<div class="row"><div class="label">🤖 Бот</div>
          <div class="value">${esc(d.bot.text)}</div></div>` : ''}
        ${d.log_chat.chat_id ? '' : `<div class="intro" style="margin-top:8px">
          ⚠️ Лог-чат не выбран: бот работает молча, карточек и кнопок нет.</div>`}
        <div style="margin-top:10px" class="wrap">${overview}</div>
      </div>

      ${(d.groups || []).map((g) => {
        // «Приколы» — не раздел настроек, а своя страница, поэтому
        // подмешиваем её к развлечениям вручную
        const own = d.sections.filter((s) => s.group === g.key);
        const extra = g.key === 'fun'
          ? tile(`#/chat/${cid}/games`, '🎪 Приколы', { dot: d.games_on }) : '';
        if (!own.length && !extra) return '';
        return `<div class="card">
          <h2>${esc(g.title)}</h2>
          <div class="intro">${esc(g.hint)}</div>
          <div class="tiles" style="margin-top:10px">
            ${own.map((s) => tile(`#/chat/${cid}/s/${s.key}`, s.title,
              s.on === null ? {} : { dot: s.on })).join('')}
            ${extra}
          </div>
        </div>`;
      }).join('')}

      <div class="card">
        <div class="tiles">
          ${tile(`#/chat/${cid}/active`, '🚫 Наказания', { sub: 'активных ' + d.active })}
          ${tile(`#/chat/${cid}/stats`, '📈 Статистика')}
          ${d.level === 'punish' ? '' : tile(`#/chat/${cid}/events`, '📜 Лог чата')}
          ${d.level !== 'owner' ? '' : `
            ${tile(`#/chat/${cid}/admins`, '👮 Админы в боте')}
            ${tile(`#/chat/${cid}/copy`, '📥 Перенести настройки')}
            <button class="tile" data-act="set-log">
              <span>📍 Лог-чат<small>${esc(d.log_chat.title || 'не задан')}</small></span></button>
            <button class="tile" data-act="chat-net">
              <span>🕸 Сетка<small>${esc(d.net ? d.net.title : 'нет')}</small></span></button>
            <button class="tile" data-act="leave" style="grid-column:1/-1">
              <span>🚪 Убрать бота из чата</span></button>`}
        </div>
      </div>`,
  };
}

/* --- раздел настроек --- */

/* «Форма» раздела: какие поля видны и что в виджетах. Не изменилась после
   сохранения — страницу можно не перерисовывать */
const sectionShape = (d) => JSON.stringify([d.key, d.fields.map((f) => [f.key, f.visible]), d.widget_data]);

async function sectionView(cid, sec) {
  const d = await api(`/chat/${cid}/section/${sec}`);
  CACHE.sectionShape = sectionShape(d);
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
      <div class="card"><div class="intro">${introHtml(d.intro)}</div></div>
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
        <div class="row"><div class="label">⛔ Спам<small>сообщения</small></div>
          <div class="value">${w.spam}</div></div>
        <div class="row"><div class="label">🕊 Норма<small>сообщения</small></div>
          <div class="value">${w.ok}</div></div>
        ${linkRow(`#/chat/${cid}/spamprofiles`, '🧪 Профили спамеров', w.faces_spam)}
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

    case 'prof_words':
      return `<div class="card">${linkRow(`#/chat/${cid}/profwords`,
        '📝 Слова для профилей', w.count)}
        ${w.count ? '' : `<div class="intro" style="margin-top:8px">Пусто —
          по словам профиль не проверяется. Список свой, отдельный от стоп-слов
          чата: в сообщениях запрещают темы, а в описании то же слово ловит и
          того, кто тему осуждает.</div>`}
      </div>`;

    case 'sub_chat':
      return `<div class="card">
        <div class="row"><div class="label">📣 Канал<small>бот должен быть в нём
          администратором</small></div>
          <div class="value">${esc(w.title || 'привязанный к чату')}</div></div>
        <button class="btn ghost wide" style="margin-top:10px" data-act="sub-chan">
          Выбрать канал</button>
      </div>`;

    case 'sub_text':
      if (!w.shown) return '';
      return `<div class="card">${linkRow(`#/chat/${cid}/answers/sub/${cid}`,
        '✉️ Сообщение в личку', w.count || 'не задано')}
        ${w.count ? '' : `<div class="intro" style="margin-top:8px">Пока не
          задано — бот ничего не напишет, а «держать и ждать» без этого
          бессмысленно: человек не узнает, чего от него хотят.</div>`}
      </div>`;

    case 'nn_shadow':
      return `<div class="card">
        <div class="row"><div class="label">📄 Теневой журнал
          <small>решения фильтра, которые ни на что не влияли</small></div>
          <div class="value">${w.on ? (w.size ? Math.round(w.size / 1024) + ' КБ' : 'пуст')
                                    : 'режим не включён'}</div></div>
        <div class="intro" style="margin-top:8px">Свой файл на чат:
          <span class="mono">${esc(w.path)}</span></div>
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
        ${tile(`#/chat/${cid}/spamprofiles`, '🧪 Спам-профили', { sub: `записей ${w.spam_profiles}` })}
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
        <h2>🗂 Кучки похожих улик</h2>
        <div class="intro">${introHtml('Бот раскладывает улики на кучки по смыслу текста — не по пометкам. Поэтому в одной кучке бывают и спам, и обычные сообщения.\n\n'
          + '«Без оценки» — наказания, выданные вручную: кнопка размечает в кучке только их, уже размеченное не меняется.\n'
          + '«Что знает бот» — то, на чём он учится. Оптом не размечается: откройте кучку и поправьте оценку у нужных сообщений.')}</div>
        <div class="row" style="margin-top:10px" id="cluster-tabs">
          <button class="btn ghost" data-act="nn-clusters" data-scope="unknown"
            data-tab="unknown">✋ Без оценки (${w.unknown})</button>
          <button class="btn ghost" data-act="nn-clusters" data-scope="profile"
            data-tab="profile">📚 Что знает бот (${w.profile})</button>
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

async function profWordsView(cid) {
  return wordsView(cid, 'prof');
}

async function wordsView(cid, kind) {
  const prof = kind === 'prof';
  const d = await api(`/chat/${cid}/words${prof ? '?kind=prof' : ''}`);
  return {
    title: prof ? 'Слова для профилей' : 'Стоп-слова',
    back: `#/chat/${cid}/s/${prof ? 'prof' : 'words'}`,
    html: `<div class="card">
      <div class="intro">${prof
        ? `Ищутся в «о себе», названии канала и его описании. Сюда идут рекламные
           метки — «в лс», «онлифанс», «18+», — а не темы разговора: в описании
           они ловят и тех, кто тему осуждает.`
        : 'Слово со звёздочкой ловит любые окончания.'}</div>
      <div class="intro" style="margin-top:8px">⚖️ Вес — сколько слово значит для будущей
        единой оценки. На нынешние наказания он не влияет.
        <b>Сильная</b> — в живой речи не встречается. <b>Слабая</b> — обычное слово
        («оплата», «пиши»), одной не хватит даже на подозрение.</div>
      <div style="margin-top:10px">
        ${d.items.map((r) => `<div class="item">
          <div class="body mono">${esc(r.label)}<small>вес: ${esc(r.weight_label)}</small></div>
          <button class="chip" data-act="word-weight" data-id="${r.id}"
                  data-weight="${r.weight}">⚖️ ${esc(r.weight_label)}</button>
          <button class="x" data-act="word-del" data-id="${r.id}">✕</button></div>`).join('')
          || '<div class="empty">Пусто.</div>'}
      </div>
      <button class="btn wide" style="margin-top:10px" data-act="words-add"
        data-kind="${prof ? 'prof' : ''}">➕ Добавить слова</button>
      ${d.items.length ? `<button class="btn wide danger" style="margin-top:8px"
        data-act="words-clear" data-kind="${prof ? 'prof' : ''}">🗑 Очистить список</button>` : ''}
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

async function spamProfilesView(cid) {
  const d = await api(`/chat/${cid}/spamprofiles`);
  return {
    title: 'Спам-профили',
    back: `#/chat/${cid}/s/watch`,
    html: `<div class="card">
      <div class="intro">${introHtml('С этими профилями бот сравнивает новых людей, когда включено «Сравнивать профили с забаненными».\n\n'
        + 'Сюда попадают профили, записанные кнопкой «Спам-профиль», и те, кого бот забанил сам. Записали по ошибке — уберите.')}</div>
    </div>
    <div class="card">
      <h2>Всего: ${d.items.length}</h2>
      ${d.items.map((p) => `<div class="item">
          <div class="body">${esc(p.who)}<small>${esc(p.when)} · ${esc(p.text)}</small></div>
          <button class="btn small ghost" data-act="spamprofile-del" data-id="${p.id}">✕ Убрать</button>
        </div>`).join('') || '<div class="empty">Пусто.</div>'}
    </div>`,
  };
}

async function adminsView(cid) {
  const d = await api(`/chat/${cid}/admins`);
  const hint = {
    punish: 'наказания, проверка статуса, массовые действия',
    settings: 'то же плюс все разделы модерации',
  };
  return {
    title: 'Админы в боте',
    back: `#/chat/${cid}`,
    html: `<div class="card">
      <div class="intro">Кого пустить в панель и меню бота по этому чату.
        Добавлять можно только админов самого чата — бот это проверяет.
        Лог-чат, сетки, перенос настроек, удаление бота и этот список
        остаются только у вас.</div>
      <div style="margin-top:10px">
        ${d.items.map((r) => `<div class="item">
            <div class="body">${esc(r.who)}
              <small>${esc(d.levels[r.level])} · ${esc(hint[r.level] || '')}</small></div>
            <button class="chip" data-act="admin-level" data-uid="${r.user_id}"
              data-level="${r.level === 'punish' ? 'settings' : 'punish'}">🔁 ${esc(d.levels[r.level])}</button>
            <button class="x" data-act="admin-del" data-uid="${r.user_id}">✕</button></div>`).join('')
          || '<div class="empty">Пока никого.</div>'}
      </div>
      <button class="btn wide" style="margin-top:10px" data-act="admin-add">➕ Добавить админа</button>
    </div>`,
  };
}

/* --- графики --- */
/*
 * Рисуем сами, без библиотек: полмегабайта чужого кода ради шести картинок в
 * мини-аппе не окупаются, а весь нужный график — это одна строка точек.
 *
 * Голая кривая без подписей — картинка «что-то росло»: непонятно ни сколько,
 * ни когда. Поэтому у каждого графика есть сетка с числами, даты под осью и
 * строка чтения: нажатие по графику показывает конкретные сутки с днём недели
 * и числом. Наведение мышью на телефоне недоступно, поэтому именно нажатие.
 */

const chartMax = (vals) => Math.max(1, ...vals);
const fmtNum = (n) => Number(n).toLocaleString('ru-RU');

function plural(n, one, few, many) {
  const t = Math.abs(n) % 100;
  if (t >= 11 && t <= 14) return many;
  const l = t % 10;
  if (l === 1) return one;
  if (l >= 2 && l <= 4) return few;
  return many;
}

/* Поле графика: сверху место под подписи сетки, снизу — под даты. */
const CW = 320, CH = 136, CPAD = 4, CTOP = 10, CBOT = 22;
const CPLOT = CH - CTOP - CBOT;
const cy = (v, max, top = CTOP, h = CPLOT) => (top + h * (1 - v / max)).toFixed(1);

/* Три линии сетки. Числа подписываем у верхней и средней: у нуля и так ясно. */
function chartGrid(max, top = CTOP, h = CPLOT) {
  return [1, 0.5, 0].map((k) => {
    const y = (top + h * (1 - k)).toFixed(1);
    return `<line x1="0" y1="${y}" x2="${CW}" y2="${y}" stroke="var(--line)" stroke-width=".7"></line>`
      + (k ? `<text x="1" y="${(Number(y) - 3).toFixed(1)}" font-size="8"
           fill="var(--hint)">${fmtNum(Math.round(max * k))}</text>` : '');
  }).join('');
}

/* Даты под осью: все, если дней мало, иначе пять штук через равные промежутки. */
function chartDates(series) {
  const n = series.length;
  const step = n <= 8 ? 1 : Math.ceil(n / 5);
  const idx = [];
  for (let i = 0; i < n; i += step) idx.push(i);
  if (idx[idx.length - 1] !== n - 1) idx.push(n - 1);
  const slot = (CW - CPAD * 2) / n;
  return idx.map((i) => {
    const x = CPAD + slot * (i + 0.5);
    const anchor = i === 0 ? 'start' : (i === n - 1 ? 'end' : 'middle');
    const label = n <= 8 ? `${series[i].dow} ${series[i].date}` : series[i].date;
    return `<text x="${x.toFixed(1)}" y="${CH - 7}" font-size="8" fill="var(--hint)"
      text-anchor="${anchor}">${esc(label)}</text>`;
  }).join('');
}

/* Выходные подсвечиваем: половина провалов в чатах — это суббота с воскресеньем. */
function chartWeekends(series) {
  const n = series.length;
  if (n > 45) return '';            // на 90 днях полоски сливаются в кашу
  const slot = (CW - CPAD * 2) / n;
  return series.map((r, i) => (r.weekend
    ? `<rect x="${(CPAD + i * slot).toFixed(1)}" y="${CTOP}" width="${slot.toFixed(1)}"
        height="${CPLOT}" fill="var(--hint)" opacity=".08"></rect>`
    : '')).join('');
}

const chartCursor = (extra = '') => `<line class="cursor" x1="0" y1="${CTOP}" x2="0"
    y2="${CTOP + CPLOT}" stroke="var(--text)" stroke-width="1" opacity=".35"
    style="display:none"></line>${extra}`;

/* Линия с заливкой: сообщения по суткам. Пунктир — среднее за период. */
function lineChart(series) {
  const vals = series.map((r) => r.msgs);
  const max = chartMax(vals);
  const n = vals.length;
  const slot = (CW - CPAD * 2) / n;
  const x = (i) => (CPAD + slot * (i + 0.5)).toFixed(1);
  const line = vals.map((v, i) => `${x(i)},${cy(v, max)}`).join(' ');
  const avg = vals.reduce((a, b) => a + b, 0) / n;
  return `<svg class="chart" viewBox="0 0 ${CW} ${CH}" data-kind="msgs" data-n="${n}" role="img">
    ${chartWeekends(series)}
    ${chartGrid(max)}
    <polygon points="${x(0)},${CTOP + CPLOT} ${line} ${x(n - 1)},${CTOP + CPLOT}"
      fill="var(--accent)" opacity=".16"></polygon>
    <polyline points="${line}" fill="none" stroke="var(--accent)" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round"></polyline>
    <line x1="0" y1="${cy(avg, max)}" x2="${CW}" y2="${cy(avg, max)}" stroke="var(--accent)"
      stroke-width="1" stroke-dasharray="3 3" opacity=".6"></line>
    ${chartCursor('<circle class="dot" r="3.2" fill="var(--accent)" stroke="var(--bg)" stroke-width="1.2" style="display:none"></circle>')}
    ${chartDates(series)}
  </svg>`;
}

/* Парные столбики: вверх пришли, вниз ушли — так виден размен, а не два ряда. */
function joinsChart(series) {
  const joins = series.map((r) => r.joins);
  const leaves = series.map((r) => r.leaves);
  const max = chartMax([...joins, ...leaves]);
  const n = series.length;
  const mid = CTOP + CPLOT / 2;
  const half = CPLOT / 2;
  const slot = (CW - CPAD * 2) / n;
  const w = Math.max(1.2, slot * 0.62);
  const bar = (v, i, up) => {
    const h = (half * (v / max)).toFixed(1);
    if (!Number(h)) return '';
    const bx = (CPAD + i * slot + (slot - w) / 2).toFixed(1);
    return `<rect x="${bx}" y="${up ? (mid - h).toFixed(1) : mid}" width="${w.toFixed(1)}"
      height="${h}" rx="${Math.min(1.5, w / 2).toFixed(1)}"
      fill="var(--${up ? 'ok' : 'danger'})"></rect>`;
  };
  return `<svg class="chart" viewBox="0 0 ${CW} ${CH}" data-kind="flow" data-n="${n}" role="img">
    ${chartWeekends(series)}
    <text x="1" y="${(CTOP + 7).toFixed(1)}" font-size="8" fill="var(--hint)">${fmtNum(max)}</text>
    <text x="1" y="${(mid + half - 1).toFixed(1)}" font-size="8" fill="var(--hint)">${fmtNum(max)}</text>
    ${joins.map((v, i) => bar(v, i, true)).join('')}
    ${leaves.map((v, i) => bar(v, i, false)).join('')}
    <line x1="0" y1="${mid}" x2="${CW}" y2="${mid}" stroke="var(--line)" stroke-width="1"></line>
    ${chartCursor()}
    ${chartDates(series)}
  </svg>`;
}

/* Сутки по часам: когда бот чаще всего наказывает. */
function hoursChart(vals) {
  const max = chartMax(vals);
  const slot = (CW - CPAD * 2) / 24;
  const w = slot * 0.66;
  return `<svg class="chart" viewBox="0 0 ${CW} ${CH}" data-kind="hours" data-n="24" role="img">
    ${chartGrid(max)}
    ${vals.map((v, h) => {
      const bh = (CPLOT * (v / max)).toFixed(1);
      const bx = (CPAD + h * slot + (slot - w) / 2).toFixed(1);
      return `<rect x="${bx}" y="${cy(v, max)}" width="${w.toFixed(1)}" height="${bh}"
        rx="1.5" fill="var(--accent)" opacity="${v ? 1 : 0.22}"></rect>`;
    }).join('')}
    ${chartCursor()}
    ${[0, 3, 6, 9, 12, 15, 18, 21].map((h) => `<text x="${(CPAD + slot * (h + 0.5)).toFixed(1)}"
      y="${CH - 7}" font-size="8" fill="var(--hint)" text-anchor="middle">${h}</text>`).join('')}
  </svg>`;
}

/* Список с полосками: типы наказаний, правила, прощения. */
function barList(items) {
  if (!items.length) return '<div class="empty">Пока пусто.</div>';
  const max = chartMax(items.map((i) => i.count));
  const total = items.reduce((a, b) => a + b.count, 0);
  return items.map((i) => `<div class="bar-row">
    <div class="bar-name">${esc(i.label)}</div>
    <div class="bar-track"><div class="bar-fill" style="width:${Math.round(i.count / max * 100)}%"></div></div>
    <div class="bar-num">${i.count}<small>${Math.round(i.count / total * 100)}%</small></div>
  </div>`).join('');
}

function chartCard(title, hint, body, read, note) {
  return `<div class="card">
    <h2>${esc(title)}</h2>
    <div class="intro">${esc(hint)}</div>
    ${body}
    ${read === null ? '' : `<div class="chart-read" data-read="${esc(read.kind)}">${esc(read.text)}</div>`}
    ${note ? `<div class="chart-note">${esc(note)}</div>` : ''}
  </div>`;
}

/* Последние загруженные ряды: по ним строится подпись при нажатии. */
let CHART_SEEN = null;

const dayRead = (r) => `${r.date}, ${r.dow} — ${fmtNum(r.msgs)} `
  + plural(r.msgs, 'сообщение', 'сообщения', 'сообщений');
const flowRead = (r) => `${r.date}, ${r.dow} — пришло ${r.joins}, ушло ${r.leaves}`;
const hourRead = (h, v) => `${String(h).padStart(2, '0')}:00 — ${v} `
  + plural(v, 'наказание', 'наказания', 'наказаний');

/* Нажатие по графику: показать точные сутки (или час) под картинкой. */
function chartPick(svg, clientX) {
  if (!CHART_SEEN) return;
  const kind = svg.dataset.kind;
  const n = Number(svg.dataset.n);
  const box = svg.getBoundingClientRect();
  const rel = Math.min(0.999, Math.max(0, (clientX - box.left) / box.width));
  // столбик занимает свою долю ширины, поэтому берём номер доли, а не ближайшую точку
  const i = Math.min(n - 1, Math.floor(rel * n));
  const read = document.querySelector(`.chart-read[data-read="${kind}"]`);
  if (kind === 'hours') {
    if (read) read.textContent = hourRead(i, CHART_SEEN.hours[i]);
  } else {
    const row = CHART_SEEN.series[i];
    if (!row) return;
    if (read) read.textContent = kind === 'msgs' ? dayRead(row) : flowRead(row);
  }
  const slot = (CW - CPAD * 2) / n;
  const x = CPAD + slot * (i + 0.5);
  const cursor = svg.querySelector('.cursor');
  if (cursor) {
    cursor.setAttribute('x1', x);
    cursor.setAttribute('x2', x);
    cursor.style.display = '';
  }
  const dot = svg.querySelector('.dot');
  if (dot && kind === 'msgs') {
    const max = chartMax(CHART_SEEN.series.map((r) => r.msgs));
    dot.setAttribute('cx', x);
    dot.setAttribute('cy', cy(CHART_SEEN.series[i].msgs, max));
    dot.style.display = '';
  }
}

function chartTouch(e) {
  const svg = e.target.closest && e.target.closest('svg.chart[data-kind]');
  if (!svg) return;
  if (e.type === 'pointermove' && !e.buttons) return;
  chartPick(svg, e.clientX);
}

document.addEventListener('pointerdown', chartTouch);
document.addEventListener('pointermove', chartTouch);

/* Рост или спад к прошлому такому же периоду. Пусто — сравнивать не с чем.
 *
 * tone = 'plain' для того, где рост не хорош и не плох: наказаний стало
 * меньше — это может быть и спокойный месяц, и выключенный фильтр, красить
 * такое в зелёное или красное значит врать. */
function trendTag(now, before, tone = 'auto') {
  if (!before) return '';
  const diff = Math.round((now - before) / before * 100);
  if (Math.abs(diff) < 3) return ' <span class="muted">без перемен</span>';
  const sign = diff > 0 ? '+' : '';
  const cls = tone === 'plain' ? 'muted' : `trend ${diff > 0 ? 'up' : 'down'}`;
  return ` <span class="${cls}">${sign}${diff}%</span>`;
}

/* Итоги периода.
 *
 * Голые суммы ни о чём не говорят: «28 наказаний» — это много или мало,
 * понятно только рядом с прошлым таким же отрезком. Поэтому каждая строка —
 * число и сравнение, а не набор любопытных фактов вроде «самых тихих суток».
 */
function chartTotals(d) {
  const t = d.totals;
  const p = d.prev || {};
  const net = t.joins - t.leaves;
  const netWas = (p.joins || 0) - (p.leaves || 0);
  const rows = [
    ['💬 Сообщений', `${fmtNum(t.msgs)}${trendTag(t.msgs, p.msgs)}`,
      `было ${fmtNum(p.msgs || 0)}`],
    ['👥 Людей в чате', `${net > 0 ? '+' : ''}${net}`,
      `пришло ${t.joins} · ушло ${t.leaves} · было ${netWas > 0 ? '+' : ''}${netWas}`],
    ['🔨 Наказаний', `${t.punished}${trendTag(t.punished, p.punished, 'plain')}`,
      `ботом ${t.punished - t.manual} · вручную ${t.manual}`],
  ];
  if (t.punished) {
    // прощение — это признанная ошибка фильтра: доля важнее самого числа
    rows.push(['🕊 Снято как ошибка', `${t.forgiven}`,
      `${Math.round(t.forgiven / t.punished * 100)}% наказаний`]);
  }
  return `<div class="card">
    <h2>📊 Итоги за период</h2>
    <div class="intro">Рядом — тот же по длине отрезок до него.</div>
    ${rows.map(([k, v, sub]) => `<div class="row"><div class="label">${esc(k)}
      <small>${esc(sub)}</small></div>
      <div class="value">${v}</div></div>`).join('')}
  </div>`;
}

async function chartsView(cid) {
  const d = await api(`/chat/${cid}/charts?days=${CHART_DAYS}`);
  CHART_DAYS = d.days;
  CHART_SEEN = d;
  const t = d.totals;
  const last = d.series[d.series.length - 1];
  const span = d.series.length ? `${d.series[0].date} — ${last.date}` : '';
  const quiet = !t.msgs && !t.joins && !t.leaves && !t.punished;
  const hotHour = d.hours.indexOf(Math.max(...d.hours));   // для подписи под часами

  return {
    title: 'Графики',
    back: `#/chat/${cid}/stats`,
    html: `
      <div class="card">
        <div class="wrap">
          ${d.ranges.map((r) => `<button class="chip ${r === d.days ? 'on' : 'off'}"
            data-act="chart-days" data-days="${r}">${r} дней</button>`).join('')}
        </div>
        <div class="chart-note" style="margin-top:8px">${esc(span)} · нажмите на график,
          чтобы увидеть точное число за день</div>
      </div>
      ${quiet ? '<div class="card"><div class="empty">За этот период бот ничего не записал.</div></div>' : `
      ${chartTotals(d)}
      ${chartCard('💬 Сообщения по дням',
        'Столько сообщений бот видел в чате каждые сутки. Пунктир — среднее, серым — выходные.',
        lineChart(d.series), { kind: 'msgs', text: dayRead(last) },
        `всего ${fmtNum(t.msgs)} · в среднем ${fmtNum(Math.round(t.msgs / d.series.length))} в сутки`)}
      ${chartCard('👥 Пришли и ушли',
        'Вверх — вступившие, вниз — вышедшие. Резкий всплеск вверх обычно и есть набег.',
        joinsChart(d.series), { kind: 'flow', text: flowRead(last) },
        `пришло ${t.joins} · ушло ${t.leaves}`)}
      ${chartCard('🔨 Наказания по типам',
        'Чего в чате больше: мутов или банов.',
        barList(d.kinds), null, `всего ${t.punished}`)}
      ${chartCard('📏 За какие правила',
        'Какое правило работает чаще всех. «Вручную» — наказания админов, «прочее» — капча, варны, набеги и жалобы.',
        barList(d.rules), null)}
      ${chartCard('🕊 Прощения по правилам',
        'Где бот ошибается: если у правила много прощений, его стоит смягчить.',
        barList(d.forgiven), null, `всего ${t.forgiven}`)}
      ${chartCard('🕒 Когда наказывают',
        'Часы суток по местному времени. Видно, когда в чате спокойно, а когда нужен живой админ.',
        hoursChart(d.hours), { kind: 'hours', text: hourRead(hotHour, d.hours[hotHour]) })}
      `}`,
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
            ${c ? esc(d.cooldown_labels[c] || c + ' сек') : 'без кулдауна'}</option>`).join('')}
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
            ${x ? esc(d.cooldown_labels[x] || x + ' сек') : 'без кулдауна'}</option>`).join('')}
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
  sub: (cid) => `#/chat/${cid}/s/sub`,
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

/* проверка статуса: своя страница, результат живёт в CACHE до ухода из чата */
function statusHtml(cid) {
  const s = CACHE.status;
  if (!s || String(s.cid) !== String(cid)) return '';
  const d = s.data;
  const counts = d.counts.map((c) => `<span class="chip stat">${esc(c.label)}: ${c.n}</span>`).join('')
    || '<span class="muted">Наказаний не было</span>';
  const chats = d.chats.length
    ? d.chats.map((c) => `<div class="item"><div class="body">
        <b>${esc(c.title)}</b><small>${esc(c.state)}</small>
        ${c.lines.map((l) => `<small>${esc(l)}</small>`).join('')}
      </div></div>`).join('')
    : '<div class="empty">Ни в одном из ваших чатов не встречался.</div>';
  const events = !d.events.length ? '' : `<div class="card">
      <h2>📜 Последние события</h2>
      ${d.events.map((e) => `<div class="item"><div class="body">
          ${esc(e.icon)} <b>${esc(e.label)}</b> · ${esc(e.chat)}
          <small>${esc(e.when)} · ${esc(e.body)}</small>
        </div></div>`).join('')}
    </div>`;
  return `<div class="card">
      <h2><a href="${esc(d.link)}" target="_blank" rel="noopener">${esc(d.name)}</a></h2>
      <div class="muted mono">${esc(d.user_id)}${d.username ? ' · @' + esc(d.username) : ''}${d.premium ? ' · ⭐ Premium' : ''}</div>
      ${d.about.concat(d.facts).map((f) => `<div class="intro">${esc(f)}</div>`).join('')}
      <h2 style="margin-top:12px">⚖️ Наказания в ваших чатах</h2>
      <div class="wrap">${counts}</div>
      <div class="wrap" style="margin-top:10px">
        <button class="btn ghost" data-act="spam-profile" data-uid="${esc(d.user_id)}">🧪 Спам-профиль${d.here ? ` в «${esc(d.here)}»` : ''}</button>
      </div>
    </div>
    <div class="card"><h2>💬 Чаты</h2>${chats}</div>
    ${events}`;
}

async function statusView(cid) {
  return {
    title: 'Проверка статуса',
    back: `#/chat/${cid}/active`,
    html: `<div class="card">
      <div class="intro">id, @username или ссылка t.me/… — покажу, в каких ваших чатах человек
        встречался, что на нём висит сейчас, сколько наказаний было и последние события.</div>
      <div class="wrap" style="margin-top:10px">
        <button class="btn" data-act="status-check">🔎 Проверить</button>
      </div>
    </div>
    ${statusHtml(cid)}`,
  };
}

async function activeView(cid) {
  // оба списка нужны сразу — ждём их вместе, а не по очереди
  const [d, f] = await Promise.all([api(`/chat/${cid}/active`),
                                    api(`/chat/${cid}/forgiven`)]);
  const forgiven = !f.items.length ? '' : `<div class="card">
      <h2>🕊 Прощённые (${f.items.length})</h2>
      <div class="intro">Этих людей правило наказало зря — вы сняли наказание и выключили
        для них именно его. Остальные проверки работают. Список растёт — значит правило
        настроено криво.</div>
      <div>
        ${f.items.map((p) => `<div class="item">
            <div class="body"><a href="${esc(p.link)}" target="_blank" rel="noopener">${esc(p.who)}</a><small>${esc(p.scope_label)} · ${esc(p.since)} · ${esc(p.reason)}</small></div>
            <button class="btn small ghost" data-act="unforgive" data-id="${p.id}">↩️ Вернуть</button>
          </div>`).join('')}
      </div>
    </div>`;
  return {
    title: 'Наказания',
    back: `#/chat/${cid}`,
    // порядок сверху вниз — от короткого действия к длинному списку: проверить
    // человека, наказать пачку, посмотреть, кто уже наказан
    html: `<div class="card">${linkRow(`#/chat/${cid}/status`, '🔎 Проверка статуса', '')}</div>
    <div class="card">
      <h2>Массовые действия</h2>
      <div class="intro">Список id или @username одним полем, через пробел или запятую.</div>
      <div class="wrap" style="margin-top:10px">
        <button class="btn ghost" data-act="mass" data-kind="unban">🔓 Разбан</button>
        <button class="btn ghost" data-act="mass" data-kind="kick">👢 Кик</button>
        <button class="btn danger" data-act="mass" data-kind="ban">⛔ Бан</button>
      </div>
    </div>
    <div class="card">
      <h2>📋 Активные (${d.items.length})</h2>
      <div>
        ${d.items.map((p) => `<div class="item">
            <div class="body"><a href="${esc(p.link)}" target="_blank" rel="noopener">${esc(p.who)}</a><small>${esc(p.kind_label)} · ${esc(p.until)}${p.since ? ` · выдан ${esc(p.since)}` : ''} · ${esc(p.reason)}</small></div>
            <button class="btn small ghost" data-act="lift" data-id="${p.id}">🔓 Снять</button>
          </div>`).join('') || '<div class="empty">Все чисты.</div>'}
      </div>
    </div>
    ${forgiven}
    <div class="card">${linkRow(`#/chat/${cid}/s/punish_cfg`, '⚙️ Настройки наказаний', '')}</div>`,
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
      <div class="intro">${introHtml(esc(g.about))}</div>
      ${g.by_hand ? `<div class="wrap" style="margin-top:10px">
        <button class="chip ${g.admins ? 'on' : ''}" data-act="game-who" data-bit="${g.bit}">
          ${g.admins ? '🛡 только админы' : '👥 все'}</button>
        <button class="chip" data-act="game-kind" data-bit="${g.bit}">🔨 ${esc(g.kind === 'ban' ? 'бан' : 'мут')}</button>
        ${g.kind === 'mute' ? `<button class="chip" data-act="game-min" data-bit="${g.bit}">⏰ ${esc(g.prize.replace('мут на ', ''))}</button>` : ''}
      </div>` : ''}
      ${g.paste ? `<div class="wrap" style="margin-top:10px">
        <button class="chip" data-act="paste-min">📏 от ${g.min} знаков</button>
        <button class="chip" data-act="paste-cd">⏰ ${esc(g.cd_label)}</button>
      </div>
      ${g.on && !g.answers ? '<div class="intro">⚠️ Заготовок нет — отвечать нечем.</div>' : ''}
      ${linkRow(`#/chat/${cid}/answers/paste/${cid}`, '🎲 Заготовки ответов', g.answers)}` : ''}
    </div>`).join('')}`,
  };
}

/* Загруженный файл настроек ждёт подтверждения: живёт вне CACHE.copy, чтобы
   перерисовка страницы не сбрасывала выбор разделов. */
let IMPORTED = null;

function importCard(cid) {
  const f = IMPORTED;
  if (!f || f.cid !== String(cid)) return '';
  const inside = Object.entries(f.inside).map(([k, n]) => `${k} ${n}`).join(', ');
  return `<div class="card">
      <h2>📥 Из файла${f.title ? ` «${esc(f.title)}»` : ''}</h2>
      <div class="intro">Настройки отмеченных разделов заменятся, списки дополнятся:
        что уже есть в чате, останется.${inside ? ` В файле: ${esc(inside)}.` : ''}</div>
      <div class="wrap" style="margin-top:10px">${f.groups.map((g) => {
        const on = f.picked.has(g.key);
        return `<button class="chip ${on ? 'on' : 'off'}" data-act="import-group" data-key="${g.key}"
          data-label="${esc(plain(g.label))}">${on ? '✓' : '○'} ${esc(plain(g.label))}</button>`;
      }).join('')}</div>
      <div class="wrap" style="margin-top:12px">
        <button class="btn" data-act="import-run">📥 Загрузить</button>
        <button class="btn ghost" data-act="import-cancel">Отмена</button>
      </div>
    </div>`;
}

async function copyView(cid) {
  const d = await api(`/chat/${cid}/copy`);
  CACHE.copy = { src: null, groups: new Set(d.groups.map((g) => g.key)) };
  return {
    title: 'Перенос настроек',
    back: `#/chat/${cid}`,
    html: `<div class="card">
      <h2>Файл настроек</h2>
      <div class="intro">Архив со всеми настройками этого чата, списками и медиа триггеров.
        Бот пришлёт его в личку. Храните как резервную копию или загрузите в другой чат.</div>
      <div class="wrap" style="margin-top:10px">
        <button class="btn ghost" data-act="settings-export">📤 Выгрузить в файл</button>
        <label class="btn ghost">📥 Загрузить из файла
          <input type="file" accept=".zip,application/zip" hidden data-upload="settings"></label>
      </div>
    </div>
    ${importCard(cid)}
    <div class="card">
      <h2>Импорт настроек из другого чата</h2>
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
  const share = (n) => (d.d7 ? ` <small>${Math.round(n / d.d7 * 100)}%</small>` : '');

  return {
    title: 'Статистика',
    back: `#/chat/${cid}`,
    html: `<div class="card">
      <div class="row"><div class="label">💬 Сообщений</div>
        <div class="value">сегодня ${fmtNum(d.d1)} · вчера ${fmtNum(d.y1)}</div></div>
      <div class="row"><div class="label">📆 За 7 дней</div>
        <div class="value">${fmtNum(d.d7)}${trendTag(d.d7, d.p7)}</div></div>
      <div class="row"><div class="label">📆 За 30 дней</div>
        <div class="value">${fmtNum(d.d30)}</div></div>
      <div class="row"><div class="label">Σ Всего</div>
        <div class="value">${fmtNum(d.total)}</div></div>
      <div class="row"><div class="label">🗣 Писали за 7 дней</div>
        <div class="value">${fmtNum(d.people7)}</div></div>
      <div class="row"><div class="label">👥 За 7 дней</div>
        <div class="value">пришло ${d.joins} · ушло ${d.leaves}</div></div>
      <div class="row"><div class="label">🔨 Наказаний</div>
        <div class="value">7д ${d.pun7} · 30д ${d.pun30}</div></div>
      ${d.since_date ? `<div class="row"><div class="label">📅 Считаем с</div>
        <div class="value">${esc(d.since_date)}</div></div>` : ''}
    </div>
    <div class="card">
      <div class="tiles">${tile(`#/chat/${cid}/charts`, '📊 Графики',
        { sub: 'за 7, 30 или 90 дней' })}</div>
    </div>
    <div class="card">
      <h2>🏆 Топ за неделю</h2>
      <div class="intro">Доля — сколько от всех сообщений недели написал человек.</div>
      ${d.top.map((t, i) => `<div class="item"><div class="body">${i + 1}. ${esc(t.who)}</div>
        <div class="value">${fmtNum(t.count)}${share(t.count)}</div></div>`).join('')
        || '<div class="empty">Пока пусто.</div>'}
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
const SEED = { label: '', q: '', page: 0, kind: 'msg' };

async function seedView() {
  const p = new URLSearchParams({ label: SEED.label, q: SEED.q,
                                  page: SEED.page, kind: SEED.kind });
  const d = await api(`/seed?${p}`);
  CACHE.seed = d;
  const prof = SEED.kind === 'prof';
  const tab = (key, name) => `<button class="btn ${SEED.label === key ? '' : 'ghost'}"
    data-act="seed-label" data-label="${key}">${name}</button>`;
  const kindTab = (key, name) => `<button class="btn ${SEED.kind === key ? '' : 'ghost'}"
    data-act="seed-kind" data-kind="${key}">${name}</button>`;
  return {
    title: 'Стартовый набор',
    back: '#/',
    html: `<div class="card">
      <div class="intro">Чужие примеры, с которых начинает молодой чат. Два вида,
        и они не смешиваются: сообщение сравнивается с сообщениями, профиль
        с профилями.<br><br>
        Набор общий: удалили пример здесь — он пропал у всех чатов сразу.</div>
      <div class="row"><div class="label">📨 Сообщения
        <small>в работе ${Math.min(d.in_work, d.msg_stats.total)}, поровну того и
        другого, и только пока чат не набрал своих ${d.until}</small></div>
        <div class="value">⛔ ${d.msg_stats.spam} · 🕊 ${d.msg_stats.ok}</div></div>
      <div class="row"><div class="label">🪪 Профили
        <small>в работе ${Math.min(d.face_seed, d.prof_stats.spam)}, не отключаются:
        рекламный профиль одинаков в любом чате</small></div>
        <div class="value">⛔ ${d.prof_stats.spam}</div></div>
      <div class="row"><div class="label">🧮 Посчитано векторов</div>
        <div class="value">${d.vecs}</div></div>
    </div>

    <div class="card">
      <div class="row" style="gap:6px">
        ${kindTab('msg', '📨 Сообщения')}${kindTab('prof', '🪪 Профили')}
      </div>
      ${prof ? '' : `<div class="row" style="gap:6px;margin-top:8px">
        ${tab('', 'Все')}${tab('spam', '⛔ Спам')}${tab('ok', '🕊 Норма')}
      </div>`}
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
        🧹 Очистить: ${prof ? 'профили' : 'сообщения'}</button>
      <div class="intro" style="margin-top:8px">Второй вид останется как был.
        ${prof ? 'Профили можно только собрать заново сборщиком.'
               : 'Сообщения грузятся с машины: <span class="mono">python tools/import_dataset.py файл</span>'}</div>
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
  [/^chat\/(-?\d+)\/profwords$/, profWordsView],
  [/^chat\/(-?\d+)\/wl\/(\d+)$/, wlEntryView],
  [/^chat\/(-?\d+)\/linkwl$/, linkwlView],
  [/^chat\/(-?\d+)\/admins$/, adminsView],
  [/^chat\/(-?\d+)\/spamprofiles$/, spamProfilesView],
  [/^chat\/(-?\d+)\/charts$/, chartsView],
  [/^chat\/(-?\d+)\/trigs$/, trigsView],
  [/^chat\/(-?\d+)\/trig\/(\d+)$/, trigView],
  [/^chat\/(-?\d+)\/cmds$/, cmdsView],
  [/^chat\/(-?\d+)\/cmd\/(\d+)$/, cmdView],
  [/^chat\/(-?\d+)\/answers\/(\w+)\/(-?\d+)$/, answersView],
  [/^chat\/(-?\d+)\/warned$/, warnedView],
  [/^chat\/(-?\d+)\/active$/, activeView],
  [/^chat\/(-?\d+)\/status$/, statusView],
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

let lastPath = null;   // что рисовали прошлый раз — чтобы не терять место на странице
const SEEN = new Set();   // какие страницы уже открывали в этот заход

/* Заготовки на время загрузки.
 *
 * Форму страницы не угадываем: после отрисовки меряем настоящие карточки и
 * запоминаем их высоты — в следующий раз заготовка повторит страницу один в
 * один, вместе с отступами. Ключ помнит ширину окна: на узком экране плитки
 * встают в столбец, и высоты другие. Храним надолго, а не до конца сессии:
 * панель закрывают и открывают заново десятки раз в день.
 *
 * Пока страницу ни разу не открывали, собираем прикидку из тех же элементов,
 * что и настоящая страница: строка сведений, пилюля, плитка, элемент списка.
 * Так совпадают и высоты, и отступы, и заготовка не подрастает при подмене. */
let CHART_DAYS = 30;

const SHAPES = {};

const shapeKey = (path) => `gremlin:shape:${Math.round(window.innerWidth / 40)}:${path}`;

function rememberShape(path) {
  const cards = [...$app.children].map((el) => Math.round(el.getBoundingClientRect().height));
  if (!cards.length || cards.some((h) => !h)) return;
  SHAPES[path] = cards;
  try { localStorage.setItem(shapeKey(path), JSON.stringify(cards)); } catch (e) { /* приватный режим */ }
}

function knownShape(path) {
  if (SHAPES[path]) return SHAPES[path];
  try {
    const raw = localStorage.getItem(shapeKey(path));
    if (raw) { SHAPES[path] = JSON.parse(raw); return SHAPES[path]; }
  } catch (e) { /* приватный режим */ }
  return null;
}

const skelCard = (inner) => `<div class="card skel">${inner}</div>`;
const skelHead = '<div class="ln head"></div>';
const skelLine = (w) => `<div class="ln" style="width:${w}%"></div>`;
const skelRows = (n) => ('<div class="row"><div class="label">'
  + '<div class="ln" style="width:55%"></div></div></div>').repeat(n);
const skelItems = (n) => ('<div class="item"><div class="body">'
  + '<div class="ln" style="width:45%"></div>'
  + '<div class="ln" style="width:75%"></div></div></div>').repeat(n);
const skelTiles = (n) => '<div class="tiles" style="margin-top:10px">'
  + '<div class="skel-tile"></div>'.repeat(n) + '</div>';
// пилюли разной ширины: ряд одинаковых читается как таблица, а не как чипы
const skelChips = (n) => '<div class="wrap" style="margin-top:10px">'
  + Array.from({ length: n }, (_, i) => `<span class="skel-chip" style="width:${72 + (i % 4) * 26}px"></span>`).join('')
  + '</div>';

function skeletonFor(path) {
  const shape = knownShape(path);
  if (shape) {
    return shape.map((h) => `<div class="card skel" style="height:${h}px"></div>`).join('');
  }
  if (path === '') return skelCard(skelHead + skelTiles(4)) + skelCard(skelHead + skelTiles(6));
  if (/^chat\/-?\d+$/.test(path)) {
    // карточка чата: название, id, владелец, строки сведений и ряд пилюль
    return skelCard(skelHead + skelLine(30) + skelLine(42) + skelRows(4) + skelChips(13))
      + skelCard(skelHead + skelLine(65) + skelTiles(6))
      + skelCard(skelHead + skelLine(65) + skelTiles(4));
  }
  if (/^chat\/-?\d+\/charts$/.test(path)) {
    // страница графиков: полоска периодов, потом карточки с картинками и полосками
    const chart = skelCard(skelHead + skelLine(88) + '<div class="skel-chart"></div>' + skelLine(35));
    const bars = skelCard(skelHead + skelLine(70) + skelRows(3));
    return skelCard(skelChips(3) + skelLine(30)) + chart + chart + bars + bars + bars + chart;
  }
  if (/^chat\/-?\d+\/s\//.test(path)) {
    // раздел: пояснение в несколько строк, потом переключатели
    return skelCard(skelHead + skelLine(95) + skelLine(88) + skelLine(60) + skelRows(4))
      + skelCard(skelHead + skelRows(2));
  }
  const lists = /^(chat\/-?\d+\/(active|events|trigs|cmds|words|profwords|warned|answers|linkwl|admins|spamprofiles)|access|seed|admin\/log)/;
  if (lists.test(path)) return skelCard(skelHead + skelItems(6));
  return skelCard(skelHead + skelRows(3));
}

async function render() {
  const path = here();
  // Та же страница после действия (сняли наказание, включили раздел) —
  // остаёмся где были. Иначе после каждого нажатия внизу длинного списка
  // приходилось листать обратно
  const keepScroll = path === lastPath;
  const y = window.scrollY;
  // результат проверки статуса живёт, пока открыта её страница: вернулся
  // позже — делаешь свежий запрос, а не смотришь на данные часовой давности
  if (!/\/status$/.test(path)) delete CACHE.status;
  for (const [re, view] of ROUTES) {
    const m = path.match(re);
    if (!m) continue;
    // страницу уже открывали — она придёт из кэша мгновенно, мигать нечем
    if (!SEEN.has(path)) $app.innerHTML = skeletonFor(path);
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
      window.scrollTo(0, keepScroll ? y : 0);
      lastPath = path;
      SEEN.add(path);
      rememberShape(path);      // размеры пригодятся следующему заходу
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
  async 'words-add'(el) {
    const kind = el.dataset.kind || '';
    const v = await ask({ title: kind ? 'Слова для профилей' : 'Стоп-слова', multiline: true,
      hint: 'Через запятую или с новой строки. <code>слово</code> — точно, <code>слово*</code> — с окончаниями.' });
    if (!v) return;
    const r = await api(`/chat/${curChat()}/words`, { json: { text: v, kind } });
    toast(`Добавлено: ${r.added}${r.dupes ? ', уже были: ' + r.dupes : ''}`);
    render();
  },

  async 'word-weight'(el) {
    const kind = location.hash.includes('/profwords') ? 'prof' : '';
    const d = await api(`/chat/${curChat()}/words${kind ? '?kind=prof' : ''}`);
    const v = await pick({ title: 'Вес слова',
      options: d.weights.map((w) => ({ value: w.value, label: `${w.label} — ${w.hint}` })) });
    if (v === null) return;
    await api(`/chat/${curChat()}/words/${el.dataset.id}/weight`, { json: { weight: +v } });
    render();
  },

  async 'word-del'(el) {
    await api(`/chat/${curChat()}/words/${el.dataset.id}`, { method: 'DELETE' });
    render();
  },

  async 'words-clear'(el) {
    const kind = el.dataset.kind || '';
    if (!await confirmAsk(kind ? 'Удалить все слова для профилей?'
                               : 'Удалить все стоп-слова?')) return;
    const r = await api(`/chat/${curChat()}/words/clear?kind=${kind}`, { json: {} });
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
        ${scope === 'unknown' ? (g.unknown ? `<div class="wrap">
          <button class="btn ghost danger" data-act="nn-label" data-i="${i}" data-n="${g.unknown}"
            data-label="spam" data-scope="${scope}">⛔ Спамом: ${g.unknown} без оценки</button>
          <button class="btn ghost good" data-act="nn-label" data-i="${i}" data-n="${g.unknown}"
            data-label="ok" data-scope="${scope}">🕊 Нормой: ${g.unknown} без оценки</button>
        </div>` : '') : `<div class="wrap">
          <button class="btn ghost" data-act="nn-items" data-i="${i}">🔍 Разобрать по одному</button>
        </div>
        <div data-items="${i}"></div>`}
      </div>`).join('');
  },

  async 'nn-items'(el) {
    const box = document.querySelector(`[data-items="${el.dataset.i}"]`);
    const r = await api(`/chat/${curChat()}/nn/clusters/${el.dataset.i}`);
    const mark = { spam: '⛔', ok: '🕊', unknown: '✋' };
    box.innerHTML = r.items.map((it) => `<div class="item" data-sample="${it.id}">
        <div class="body"><span data-mark>${mark[it.label] || '?'}</span> ${esc(it.text.slice(0, 200))}</div>
        <div class="wrap" style="flex:none">
          <button class="chip ${it.label === 'spam' ? 'on' : 'off'}" data-act="nn-sample"
            data-id="${it.id}" data-label="spam">⛔</button>
          <button class="chip ${it.label === 'ok' ? 'on' : 'off'}" data-act="nn-sample"
            data-id="${it.id}" data-label="ok">🕊</button>
        </div>
      </div>`).join('') || '<div class="empty">Пусто.</div>';
  },

  async 'nn-sample'(el) {
    await api(`/chat/${curChat()}/nn/sample/${el.dataset.id}`, { json: { label: el.dataset.label } });
    const row = el.closest('[data-sample]');
    row.querySelectorAll('[data-act="nn-sample"]').forEach((b) => {
      const on = b.dataset.label === el.dataset.label;
      b.classList.toggle('on', on);
      b.classList.toggle('off', !on);
    });
    row.querySelector('[data-mark]').textContent = el.dataset.label === 'spam' ? '⛔' : '🕊';
    toast('Поправлено');
  },

  async 'nn-label'(el) {
    const what = el.dataset.label === 'spam' ? 'спамом' : 'нормой';
    if (!await confirmAsk(`Пометить ${what} ${el.dataset.n} сообщений без оценки?`)) return;
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

  'chart-days'(el) {
    CHART_DAYS = Number(el.dataset.days);
    render();
  },

  async 'admin-add'() {
    const v = await ask({
      title: 'Пустить в бот',
      hint: 'id или @username. Человек должен быть админом чата.',
    });
    if (!v) return;
    toast((await api(`/chat/${curChat()}/admins`, { json: { target: v } })).note);
    render();
  },

  async 'admin-level'(el) {
    await api(`/chat/${curChat()}/admins`, {
      json: { user_id: Number(el.dataset.uid), level: el.dataset.level },
    });
    render();
  },

  async 'admin-del'(el) {
    if (!await confirmAsk('Убрать доступ?')) return;
    await api(`/chat/${curChat()}/admins/${el.dataset.uid}`, { method: 'DELETE' });
    render();
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

  async 'spam-profile'(el) {
    const r = await api(`/chat/${curChat()}/spamprofile`, { json: { user_id: +el.dataset.uid } });
    toast(r.note);
    if (r.ok) el.remove();
  },

  async 'status-check'() {
    const q = await ask({ title: 'Проверка статуса', ok: 'Проверить',
      hint: 'id, @username или ссылка t.me/…' });
    if (!q) return;
    const r = await api(`/chat/${curChat()}/status?q=${encodeURIComponent(q)}`);
    if (!r.ok) { toast(r.error); return; }
    CACHE.status = { cid: curChat(), data: r };
    render();
  },

  async 'spamprofile-del'(el) {
    if (!await confirmAsk('Убрать профиль из базы спама?')) return;
    await api(`/chat/${curChat()}/spamprofiles/${el.dataset.id}`, { method: 'DELETE' });
    toast('Убран');
    render();
  },

  async unforgive(el) {
    await api(`/chat/${curChat()}/forgiven/${el.dataset.id}`, { method: 'DELETE' });
    toast('Правило снова работает');
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

  async 'paste-min'() {
    const d = await api(`/chat/${curChat()}/games`);
    const v = await pick({ title: 'С какой длины считать пастой',
      options: d.paste_mins.map((n) => ({ value: n, label: `${n} знаков` })) });
    if (v === null) return;
    await api(`/chat/${curChat()}/games/paste`, { json: { min: +v } });
    render();
  },

  async 'paste-cd'() {
    const d = await api(`/chat/${curChat()}/games`);
    const v = await pick({ title: 'Пауза между ответами', options: d.paste_cds });
    if (v === null) return;
    await api(`/chat/${curChat()}/games/paste`, { json: { cd: +v } });
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

  async 'settings-export'() {
    toast((await api(`/chat/${curChat()}/export`, { method: 'POST' })).note);
  },

  'import-group'(el) {
    const set = IMPORTED.picked;
    const on = !set.has(el.dataset.key);
    if (on) set.add(el.dataset.key); else set.delete(el.dataset.key);
    el.classList.toggle('on', on);
    el.classList.toggle('off', !on);
    el.textContent = (on ? '✓ ' : '○ ') + el.dataset.label;
  },

  async 'import-run'() {
    if (!IMPORTED.picked.size) return toast('Не выбрано ни одного раздела');
    if (!await confirmAsk('Загрузить выбранные разделы в этот чат?')) return;
    const r = await api(`/chat/${curChat()}/import/apply`, { json: { groups: [...IMPORTED.picked] } });
    IMPORTED = null;
    toast(Object.entries(r.copied).filter(([, n]) => n).map(([k, n]) => `${k}: ${n}`).join(' · ') || 'Готово');
    go(`#/chat/${curChat()}`);
  },

  'import-cancel'() {
    IMPORTED = null;
    render();
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

  async 'fold'(el) {
    const key = el.dataset.key;
    if (FOLDED.has(key)) FOLDED.delete(key);
    else FOLDED.add(key);
    saveFolded();
    await render();
  },

  async 'fold-all'() {
    const groups = groupByOwner(INIT.chats);
    // свёрнуты все — разворачиваем; иначе сворачиваем всё
    if (groups.every((g) => FOLDED.has(g.key))) FOLDED.clear();
    else groups.forEach((g) => FOLDED.add(g.key));
    saveFolded();
    await render();
  },

  async 'sub-chan'() {
    const v = await ask({ title: 'Канал для подписки',
                          hint: '@юзернейм или id канала. «-» — вернуть привязанный к чату.' });
    if (v === null) return;
    await api(`/chat/${curChat()}/sub-chat`, { json: { target: v.trim() } });
    toast('Сохранено');
    await render();
  },

  async 'seed-kind'(el) {
    SEED.kind = el.dataset.kind === 'prof' ? 'prof' : 'msg';
    // у профилей «нормы» не бывает — фильтр по метке сбрасываем,
    // иначе список окажется пустым без видимой причины
    if (SEED.kind === 'prof') SEED.label = '';
    SEED.page = 0;
    await render();
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
    const r = await api('/seed/delete', { json: { ids: [Number(el.dataset.id)], kind: SEED.kind } });
    toast(`Удалено: ${r.gone}`);
    await render();
  },

  async 'seed-wipe'() {
    const n = (CACHE.seed && CACHE.seed.total) || 0;
    if (!await confirmAsk(`Удалить ${n} ${num(n, 'пример', 'примера', 'примеров')} `
                       + `по «${SEED.q}»? Они пропадут у всех чатов.`)) return;
    const r = await api('/seed/delete',
                        { json: { label: SEED.label, q: SEED.q, kind: SEED.kind } });
    toast(`Удалено: ${r.gone}`);
    SEED.q = '';
    SEED.page = 0;
    await render();
  },

  async 'seed-clear'() {
    const n = (CACHE.seed && CACHE.seed.stats.total) || 0;
    const what = SEED.kind === 'prof' ? 'профилей' : 'сообщений';
    if (!await confirmAsk(`Удалить все ${n} ${what}? Второй вид останется.`)) return;
    const r = await api('/seed/delete', { json: { all: true, kind: SEED.kind } });
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
  hapticPick();
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
  // второе нажатие, пока идёт первое, — это второй бан и вторая рассылка
  if (actEl.dataset.busy) return;
  actEl.dataset.busy = '1';
  actEl.disabled = true;
  try {
    await fn(actEl);
  } catch (err) {
    hapticDone(false);
    toast(err.message);
  } finally {
    delete actEl.dataset.busy;
    actEl.disabled = false;
  }
});

document.addEventListener('change', async (e) => {
  const el = e.target;
  try {
    if (el.dataset.toggle !== undefined || el.dataset.select !== undefined) {
      const toggle = el.dataset.toggle !== undefined;
      const key = toggle ? el.dataset.toggle : el.dataset.select;
      const r = await api(`/chat/${curChat()}/set`,
        { json: { key, value: toggle ? (el.checked ? 1 : 0) : el.value } });
      if (toggle) hapticPick();
      // Браузер уже показал новое значение — перерисовка нужна, только если от
      // него что-то зависит: появилось или пропало другое поле, поменялся виджет.
      // Иначе страница мигала и прыгала на каждое нажатие
      const page = here().match(/^chat\/-?\d+\/s\/(\w+)$/);
      if (!page || page[1] !== r.key || sectionShape(r) !== CACHE.sectionShape) render();
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
    } else if (el.dataset.upload === 'settings') {
      const fd = new FormData();
      fd.append('file', el.files[0]);
      el.value = '';
      const r = await api(`/chat/${curChat()}/import`, { form: fd });
      IMPORTED = { cid: String(curChat()), ...r, picked: new Set(r.groups.map((g) => g.key)) };
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
    hapticDone(false);
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
