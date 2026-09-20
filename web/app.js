/* Basis Terminal —— 前端逻辑
   只消费 /api/*；无外部依赖（图表为手写 SVG，离线可用）。

   改版（docs/48）：
     ① 3 个视图 + 1 个「全部」兜底 → URL hash 深链，可前进后退
     ② 顶栏 + 视图切换 + KPI 条合成 sticky 全局栏（关键指标不再随滚动消失）
     ③ 长文字按 T1/T2/T3 分层，T2「口径」默认收起（原生 <details>，键盘可用）
     ④ 决策表：结论行 56px（原 236px），理由按「警告 / 事实 / 条件」分组后按需展开
     ⑤ 动效只做 4 件：视图切换 / 数字更新高亮 / 骨架屏 / 折叠，全部尊重 reduced-motion */

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 20000;

function fmt(v, d = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return Number(v).toFixed(d);
}
function cls(v) { return v > 0 ? 'pos' : (v < 0 ? 'neg' : ''); }
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
async function api(path) {
  const r = await fetch(path, { cache: 'no-store' });
  if (!r.ok) throw new Error(path + ' -> HTTP ' + r.status);
  return r.json();
}
function reduced() {
  return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

/* 数据缓存：先存进 DATA，再按「当前可见的视图」渲染。
   好处是切换视图零网络等待（10 行表格重绘 <1ms），也避免重绘隐藏 DOM。 */
const DATA = { overview: null, sessions: null, status: null, assess: null, alerts: null, opps: null };

/* ---------------- 视图路由 ---------------- */

const VIEWS = ['monitor', 'decision', 'evidence', 'all'];
const VIEW_TITLE = { monitor: '现在看盘', decision: '该不该做', evidence: '凭什么信', all: '全部' };

function currentView() {
  let h = (location.hash || '').replace(/^#/, '');
  try { h = decodeURIComponent(h); } catch (e) { /* 保持原样 */ }
  return VIEWS.indexOf(h) >= 0 ? h : 'monitor';
}
function isVisible(v) { const cur = currentView(); return cur === 'all' || cur === v; }

/* 切视图：写 hash（可后退）-> hashchange -> applyView */
function go(v) {
  if (currentView() === v) { applyView(v); return; }
  location.hash = v;
}

function applyView(v, opts) {
  opts = opts || {};
  const views = document.querySelectorAll('.view');
  views.forEach((el) => {
    el.hidden = !(v === 'all' || el.dataset.view === v);
    el.classList.remove('on');
  });
  if (opts.animate !== false) {
    void document.body.offsetWidth;              // 强制 reflow，让动画能重启动
    views.forEach((el) => { if (!el.hidden) el.classList.add('on'); });
  }
  document.querySelectorAll('#viewtabs button[role="tab"]').forEach((b) => {
    const sel = (b.dataset.view === v);
    b.setAttribute('aria-selected', String(sel));
    b.tabIndex = sel ? 0 : -1;
  });
  renderAll();
  if (opts.scroll !== false && window.scrollY > 8) {
    window.scrollTo({ top: 0, behavior: reduced() ? 'auto' : 'smooth' });
  }
  document.title = 'Basis Terminal · ' + (VIEW_TITLE[v] || v);
}

function initTabs() {
  const nav = $('viewtabs');
  if (!nav) return;
  const btns = Array.prototype.slice.call(nav.querySelectorAll('button[role="tab"]'));
  btns.forEach((b) => b.addEventListener('click', () => go(b.dataset.view)));
  nav.addEventListener('keydown', (e) => {
    const i = btns.indexOf(document.activeElement);
    if (i < 0) return;
    let n = null;
    if (e.key === 'ArrowRight') n = (i + 1) % btns.length;
    else if (e.key === 'ArrowLeft') n = (btns.length + i - 1) % btns.length;
    else if (e.key === 'Home') n = 0;
    else if (e.key === 'End') n = btns.length - 1;
    if (n === null) return;
    e.preventDefault();
    btns[n].focus();
    go(btns[n].dataset.view);
  });
}

/* ---------------- 数字更新高亮 ----------------
   ⚠️ 刻意不用红/绿：本项目 --pos/--neg 是"正/负"语义，
   拿来表示"变大/变小"会和基差颜色打架。用中性强调色（见 styles.css .bump）。 */
function setNum(el, text) {
  if (!el) return;
  const changed = (el.dataset.init === '1' && el.textContent !== text);
  el.textContent = text;
  el.dataset.init = '1';
  if (changed && !reduced()) {
    el.classList.remove('bump');
    void el.offsetWidth;
    el.classList.add('bump');
    setTimeout(() => el.classList.remove('bump'), 720);
  }
}
function setText(id, text) { const el = $(id); if (el) el.textContent = text; }

/* ---------------- 折叠（原生 details）+ 状态持久化 ----------------
   ⚠️ 用原生 <details>：键盘 Enter/Space、读屏播报全部免费。
   ⚠️ 高度过渡交给 CSS 的 grid/fr，不写死 max-height（否则截断长文本，
      直接踩 tools/ui_probe.js 的 clipped 红线）。 */
const FOLD_KEY = 'basis.fold.';

function foldStore(k, v) {
  try {
    if (v === undefined) return localStorage.getItem(FOLD_KEY + k);
    localStorage.setItem(FOLD_KEY + k, v);
  } catch (e) { /* 隐私模式下 localStorage 可能不可用，忽略 */ }
  return null;
}

function initFolds() {
  document.querySelectorAll('details.fold').forEach((d) => {
    const k = d.dataset.fold;
    if (k) {
      const saved = foldStore(k);
      if (saved === '1') d.open = true;
      else if (saved === '0') d.open = false;
    }
    d.addEventListener('toggle', () => {
      if (k) foldStore(k, d.open ? '1' : '0');
      syncFoldAll();
    });
  });
  const btn = $('foldall');
  if (btn) btn.addEventListener('click', toggleAllFolds);
  syncFoldAll();
}

function visibleFolds() {
  return Array.prototype.slice.call(document.querySelectorAll('details.fold'))
    .filter((d) => !d.closest('.view') || !d.closest('.view').hidden);
}

function syncFoldAll() {
  const btn = $('foldall');
  if (!btn) return;
  const fs = visibleFolds();
  const allOpen = fs.length > 0 && fs.every((d) => d.open);
  btn.textContent = allOpen ? '收起口径' : '展开口径';
}

function toggleAllFolds() {
  const fs = visibleFolds();
  const allOpen = fs.length > 0 && fs.every((d) => d.open);
  fs.forEach((d) => {
    d.open = !allOpen;
    if (d.dataset.fold) foldStore(d.dataset.fold, d.open ? '1' : '0');
  });
  syncFoldAll();
}

/* ---------------- 顶栏 & 主题陈述 ---------------- */

async function loadHealth() {
  const h = await api('/api/health');
  const closed = h.session === 'closed';
  const lab = $('session-label');
  lab.textContent = h.session_label;
  lab.className = 'sess-label ' + (closed ? 'closed' : 'open');

  // ⭐ 平台路由才是"能不能靠挂单省点差"的判据（与 session 口径相差约 4 小时）
  const rl = $('route-label');
  if (rl) {
    const mk = h.maker_benefit;
    rl.textContent = mk ? '所内撮合 · 可赚点差' : 'StockRoute · 挂单也按 Taker';
    rl.className = 'sess-label ' + (mk ? 'open' : 'closed');
    rl.title = h.route_label || '';
  }

  // 时间显示交给独立时钟（tickClock，每秒一次），不依赖健康检查的 30 秒节奏
  CLOCK_OFFSET_MS = new Date(h.server_time_utc).getTime() - Date.now();
  $('live-dot').className = 'dot on';

  setText('window-title', h.maker_benefit
    ? '当前处于「所内撮合」窗口 —— 这是策略唯一可交易的时段'
    : (closed ? '美股休市，但走 StockRoute —— 挂单也按 Taker 计费'
              : '美股开市中 —— 走 StockRoute，挂单省不了点差'));
  setText('window-body', h.maker_benefit
    ? '周末/节假日窗口：平台启用所内撮合，区分 Maker/Taker。现货腿挂单可「赚」半幅点差 —— 这是策略的收益来源。'
    : '常规交易时段：平台走 StockRoute 直连美股，所有订单按 Taker 计费（不分挂单/吃单）。此时段不宜做市，数据仅作对照基准。');
  setText('footer-meta',
    '刷新间隔 ' + REFRESH_MS / 1000 + 's · 行情缓存 ' + h.tick_seconds + 's · 配对 ' + h.pairs + ' 组');
}

/* ---------------- 时钟（每秒走字，独立于网络请求） ----------------
   之前时间只在健康检查时写一次（30 秒才刷），看起来像"卡住了"。
   改为纯前端每秒自增：不产生任何服务端请求，也不受网络抖动影响。 */

let CLOCK_OFFSET_MS = 0;

function pad(n) { return n < 10 ? '0' + n : '' + n; }

function tickClock() {
  const now = new Date(Date.now() + CLOCK_OFFSET_MS);
  const utc = pad(now.getUTCHours()) + ':' + pad(now.getUTCMinutes()) + ':' + pad(now.getUTCSeconds());
  const local = pad(now.getHours()) + ':' + pad(now.getMinutes()) + ':' + pad(now.getSeconds());
  const el = $('session-time');
  if (el) el.textContent = 'UTC ' + utc + ' · 北京时间 ' + local;
}

/* ---------------- KPI 条 ---------------- */

function updateKPI(ov) {
  const basisVals = ov.map((r) => r.basis_bp).filter((v) => typeof v === 'number');
  const spotVals = ov.map((r) => r.spot && r.spot.spread_bp).filter((v) => typeof v === 'number');
  const perpVals = ov.map((r) => r.perp && r.perp.spread_bp).filter((v) => typeof v === 'number');
  const med = (a) => {
    if (!a.length) return null;
    const s = [...a].sort((p, q) => p - q);
    return s[Math.floor(s.length / 2)];
  };
  setNum($('m-pairs'), String(ov.length));
  setNum($('m-basis'), fmt(med(basisVals)));
  setNum($('m-spot'), fmt(med(spotVals)));
  setNum($('m-perp'), fmt(med(perpVals)));
}

function updateFreshness(st) {
  const s = st && st.sampler;
  if (!s || !s.last_utc) { setNum($('m-fresh'), '无心跳'); return; }
  const min = Math.round((Date.now() - new Date(s.last_utc).getTime()) / 60000);
  if (min <= 5) setNum($('m-fresh'), min + ' 分钟');
  else if (min < 1440) setNum($('m-fresh'), Math.round(min / 60) + ' 小时');
  else setNum($('m-fresh'), Math.round(min / 1440) + ' 天');
}

/* ---------------- 配对表 ---------------- */

function renderPairs(rows) {
  const tbody = document.querySelector('#pairs-table tbody');
  if (!tbody) return;
  if (!rows || !rows.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty">暂无数据</td></tr>';
    return;
  }
  // ⚠️ 「基差最大」只在**可交易**的标的中评。
  // 实测 RSOXLUSDT 的现货盘口在交易所侧就是空的（code=00000 但 bids=0/asks=0），
  // 双腿策略在它上面根本下不了单 —— 若仍按 |基差| 排序，它的 -265bp 会永远霸榜，
  // 等于把读者往一个做不了的标的上引。
  const tradableRows = rows.filter(
    (r) => !(r.capacity && r.capacity.tradable === false));
  const maxAbs = Math.max(
    ...tradableRows.map((r) => Math.abs(r.basis_bp || 0)), 1e-9);

  tbody.innerHTML = rows.map((r) => {
    const s = r.spot, p = r.perp;
    const sBp = s ? s.spread_bp : null;
    const pBp = p ? p.spread_bp : null;
    const basis = r.basis_bp;
    // ⚠️ 不可交易的标的**不参与**「基差最大」评选（连自己的那一次也不行）：
    //    它的 |基差| 往往是全场最大，若只把它从 maxAbs 里剔除、却仍按 maxAbs 判自己，
    //    它照样会拿到标签（实测踩到）。
    const isTradable = !(r.capacity && r.capacity.tradable === false);
    const hot = isTradable && Math.abs(basis || 0) >= maxAbs * 0.98;
    const ratio = (sBp && pBp) ? (sBp / Math.max(pBp, 1e-9)) : null;
    // ---- 容量告警（09-14 修正）----
    // 旧判据：perp_top_depth_usd < 5000 —— 只看**永续腿**、且只看**最优一档**。
    // 实测该判据会把 10 个标的**全部**标成"深度不足"，而实际有 6 个够吃：
    //   TSLA 顶深 $97 -> ≤5bp 实际可吃 $26,957（低估 278 倍）
    //   NVDA 顶深 $665 -> $23,746 ｜ SOXL 顶深 $1,115 -> $174,386
    // 根因：深度是可以往下吃的，只看一档完全失真；而且策略**两条腿都要成交**。
    // 新判据：用**5 档累计**的 `depth_within_5bp_usd`（已在后端算好，取四个方向最薄者），
    // 不足 5000 才算"深度不足"，并把**瓶颈腿**一起显示出来。
    //
    // 09-19 再修两处（都是实测踩出来的）：
    //   ① 缺一条腿（`tradable === false`）时**不显示容量数字**，改标「无现货盘口」——
    //      否则会拿只有一个 venue 的数假装能成交（实测 RSOXLUSDT 现货盘口在
    //      交易所侧就是空的：code=00000 但 bids=0/asks=0）；
    //   ② 单点快照会忽好忽坏（实测 GOOGL 相邻两天中位 16,572 -> 76），
    //      所以把**窗口内不足占比**一并显示，让人能分清"永远做不了"和"时好时坏"。
    const cap = r.capacity || null;
    const eatable = cap ? cap.depth_within_5bp_usd : null;
    const topDepth = cap ? cap.min_top_depth_usd : null;
    const binding = cap ? cap.binding_leg_top : null;
    const notTradable = cap ? (cap.tradable === false) : false;
    const hasRatio = !!(cap && !notTradable &&
      cap.thin_ratio !== null && cap.thin_ratio !== undefined);
    const thin = !notTradable && ((eatable !== null && eatable !== undefined)
      ? eatable < 5000
      : (topDepth !== null && topDepth !== undefined && topDepth < 5000));
    const thinWhy = binding ? '（瓶颈：' + (binding === 'spot' ? '现货腿' : '永续腿') + '）' : '';
    const winWhy = hasRatio
      ? '；最近 ' + (cap.window_rounds || 0) + ' 轮里 ' +
        Math.round(100 * cap.thin_ratio) + '% 不足，中位 ' + fmt(cap.d5_median, 0) + ' USD'
      : '';
    const thinTitle = '≤5bp 滑点内可吃 ' + fmt(eatable, 0) + ' USD' + thinWhy + winWhy;
    const missLeg = notTradable ? (cap.missing_leg === 'spot' ? '现货' : '永续') : '';
    const missTitle = '该标的缺' + missLeg + '腿盘口（交易所侧为空），两腿策略无法建仓' + winWhy;
    return '<tr class="' + (hot ? 'best' : '') + (notTradable ? ' untradable' : '') + '">' +
      '<td class="base-name">' + esc(r.base) +
        (hot ? ' <span class="tag hot">基差最大</span>' : '') +
        (notTradable
          ? ' <span class="tag miss" title="' + esc(missTitle) + '">无' + missLeg + '盘口</span>'
          : (thin ? ' <span class="tag thin" title="' + esc(thinTitle) +
            '">深度不足</span>' : '')) +
        (hasRatio ? ' <span class="tag ratio" title="' + esc(thinTitle) +
            '">窗口不足 ' + Math.round(100 * cap.thin_ratio) + '%</span>' : '') +
      '</td>' +
      '<td>' + fmt(s && s.mid) + '</td>' +
      '<td class="sep ' + (sBp > 10 ? 'neg' : '') + '">' + fmt(sBp) +
        (ratio ? ' <span class="mono-dim">(' + ratio.toFixed(1) + '×)</span>' : '') + '</td>' +
      '<td>' + fmt(p && p.mid) + '</td>' +
      '<td class="sep">' + fmt(pBp) + '</td>' +
      '<td class="sep ' + cls(basis) + '"><strong>' + fmt(basis) + '</strong></td>' +
      '<td>' + (basis === null ? '—'
        : '<span class="tag">' + (basis > 0 ? '多现货 / 空永续' : '空现货 / 多永续') + '</span>') + '</td>' +
      // 显示"≤5bp 实际可吃"，因为那才是决定能做多大规模的量
      // ⚠️ 缺一条腿时**不许**显示数字（那会读成"容量很大"），改成明确的不可交易
      '<td>' + (notTradable
        ? '<span class="neg">不可交易</span>'
        : (cap
          ? (fmt(eatable, 0) + (binding ? ' <span class="mono-dim">' +
              (binding === 'spot' ? '现' : '永') + '</span>' : ''))
          : '—')) + '</td>' +
      '</tr>';
  }).join('');
}

/* ---------------- 分时段表 ---------------- */

function renderSessions(rows) {
  const tbody = document.querySelector('#session-table tbody');
  if (!tbody) return;
  if (!rows || !rows.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">暂无数据</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map((r) => {
    const n = (v, k) => (v === null || v === undefined) ? '—'
      : fmt(v) + ' <span class="mono-dim">n=' + (r[k] || 0) + '</span>';
    const ratio = r.ratio;
    const ratioCls = ratio === null ? '' : (ratio >= 3 ? 'neg' : 'pos');
    return '<tr>' +
      '<td class="base-name">' + esc(r.base) + '</td>' +
      '<td>' + n(r.closed, 'closed_n') + '</td>' +
      '<td>' + n(r.premarket, 'premarket_n') + '</td>' +
      '<td>' + n(r.intraday, 'intraday_n') + '</td>' +
      '<td>' + n(r.afterhours, 'afterhours_n') + '</td>' +
      '<td class="sep ' + ratioCls + '"><strong>' +
        (ratio === null ? '待盘中样本' : ratio.toFixed(2) + '×') + '</strong></td>' +
      '</tr>';
  }).join('');
}

/* ---------------- 数据状态 ---------------- */

function renderStatus(st) {
  const box = $('status-body');
  if (!box) return;
  const s = st.sampler;
  const rows = st.spread_rows || 0;
  const ageMin = s && s.last_utc
    ? Math.round((Date.now() - new Date(s.last_utc).getTime()) / 60000) : null;
  const alive = ageMin !== null && ageMin <= 5;

  const raw = st.raw || {};
  const rawLines = Object.keys(raw).sort().map((g) => {
    const info = raw[g];
    const n = Object.keys(info).length;
    const tot = Object.values(info).reduce((a, b) => a + (b.rows || 0), 0);
    const gaps = Object.values(info).reduce((a, b) => a + (b.gaps || 0), 0);
    return '<div class="stat-row"><span class="stat-k">历史 ' + esc(g) +
      ' <span class="mono-dim">(' + n + ' 文件)</span></span>' +
      '<span class="stat-v">' + tot.toLocaleString() + ' 根 · 缺口 ' + gaps + '</span></div>';
  }).join('');

  box.innerHTML =
    '<div class="stat-row"><span class="stat-k">采样器心跳</span><span class="stat-v ' +
      (s ? (alive ? 'badge-ok' : 'badge-warn') : 'badge-bad') + '">' +
      (s ? (alive ? '运行中 · ' + ageMin + ' 分钟前' : '已停止 · ' + ageMin + ' 分钟前')
         : '无心跳') + '</span></div>' +
    '<div class="stat-row"><span class="stat-k">采样轮数 / 行数</span><span class="stat-v">' +
      (s ? (s.cycles + ' 轮') : '—') + '</span></div>' +
    '<div class="stat-row"><span class="stat-k">盘口采样总行数</span><span class="stat-v">' +
      rows.toLocaleString() + '</span></div>' +
    rawLines +
    '<div class="stat-row"><span class="stat-k">局限</span><span class="stat-v">' +
      '现货 1min 零成交分钟不上线（缺口需按时间戳交集对齐，禁用前向填充）' +
    '</span></div>';
}

/* ---------------- 手写 SVG 折线图 ---------------- */

let TL = [];          // 时间序列缓存
let CHART_BASE = null; // 当前展示标的

function renderToolbar(bases) {
  const bar = $('chart-toolbar');
  if (!bar) return;
  const items = ['全部', ...bases];
  bar.innerHTML = items.map((b) =>
    '<span class="chip' + ((CHART_BASE === (b === '全部' ? null : b)) ? ' active' : '') +
    '" data-base="' + esc(b) + '">' + esc(b) + '</span>').join('');
  bar.querySelectorAll('.chip').forEach((el) => {
    el.onclick = () => {
      const v = el.dataset.base;
      CHART_BASE = (v === '全部') ? null : v;
      renderToolbar(bases);
      drawChart();
    };
  });
}

function drawChart() {
  const svg = $('chart');
  if (!svg) return;
  const W = 1000, H = 320, PAD = { t: 18, r: 54, b: 30, l: 54 };
  if (!TL.length) {
    svg.innerHTML = '<text x="500" y="160" text-anchor="middle" fill="#5c6478" ' +
      'font-size="14">等待采样数据…</text>';
    return;
  }

  // 取值序列
  const pick = (pt, field) => {
    if (CHART_BASE) return pt[field][CHART_BASE];
    const vals = Object.values(pt[field]).filter((v) => typeof v === 'number');
    if (!vals.length) return undefined;
    return vals.reduce((a, b) => a + b, 0) / vals.length;   // 多标的取均值
  };
  const basis = TL.map((p) => pick(p, 'basis'));
  const spread = TL.map((p) => pick(p, 'spread'));

  const all = basis.concat(spread).filter((v) => typeof v === 'number');
  if (!all.length) {
    svg.innerHTML = '<text x="500" y="160" text-anchor="middle" fill="#5c6478" ' +
      'font-size="14">该标的暂无数据</text>';
    return;
  }
  let lo = Math.min(...all), hi = Math.max(...all);
  const padY = (hi - lo) * 0.12 || 1;
  lo -= padY; hi += padY;

  const x = (i) => PAD.l + (W - PAD.l - PAD.r) * (TL.length === 1 ? 0.5 : i / (TL.length - 1));
  const y = (v) => PAD.t + (H - PAD.t - PAD.b) * (1 - (v - lo) / (hi - lo));

  let out = '';

  // 休市窗口底纹
  let bandStart = null;
  TL.forEach((pt, i) => {
    const isClosed = pt.session === 'closed';
    if (isClosed && bandStart === null) bandStart = i;
    if ((!isClosed || i === TL.length - 1) && bandStart !== null) {
      const x0 = x(bandStart), x1 = x(i);
      if (x1 - x0 > 1.5) {
        out += '<rect x="' + x0.toFixed(1) + '" y="' + PAD.t + '" width="' +
          (x1 - x0).toFixed(1) + '" height="' + (H - PAD.t - PAD.b) +
          '" fill="#2a2f45" opacity="0.5"/>';
      }
      bandStart = null;
    }
  });

  // Y 轴网格
  for (let k = 0; k <= 4; k++) {
    const v = lo + (hi - lo) * k / 4;
    const yy = y(v);
    out += '<line x1="' + PAD.l + '" y1="' + yy.toFixed(1) + '" x2="' + (W - PAD.r) +
      '" y2="' + yy.toFixed(1) + '" stroke="#242b3d" stroke-width="1"/>';
    out += '<text x="' + (PAD.l - 8) + '" y="' + (yy + 4).toFixed(1) +
      '" text-anchor="end" fill="#5c6478" font-size="11">' + v.toFixed(1) + '</text>';
  }
  // 零线
  if (lo < 0 && hi > 0) {
    out += '<line x1="' + PAD.l + '" y1="' + y(0).toFixed(1) + '" x2="' + (W - PAD.r) +
      '" y2="' + y(0).toFixed(1) + '" stroke="#3a4256" stroke-width="1" stroke-dasharray="4 3"/>';
  }

  const line = (arr, color) => {
    let d = '', open = false;
    arr.forEach((v, i) => {
      if (typeof v !== 'number') { open = false; return; }
      d += (open ? ' L' : ' M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1);
      open = true;
    });
    return d ? '<path d="' + d + '" fill="none" stroke="' + color +
      '" stroke-width="1.8" stroke-linejoin="round"/>' : '';
  };
  out += line(spread, '#ffb74d');
  out += line(basis, '#4c8dff');

  // X 轴时间标签（首/中/末）
  [0, Math.floor(TL.length / 2), TL.length - 1].forEach((i) => {
    if (!TL[i]) return;
    out += '<text x="' + x(i).toFixed(1) + '" y="' + (H - 9) +
      '" text-anchor="middle" fill="#5c6478" font-size="11">' +
      TL[i].ts_utc.slice(11, 16) + '</text>';
  });

  svg.innerHTML = out;
  const mode = CHART_BASE ? CHART_BASE : '全部标的均值';
  setText('chart-hint', mode + ' · ' + TL.length + ' 个采样点');
}

/* ================= 执行决策：风险与理由（项目二） =================
   改版（docs/48 §3.5）实测依据：原表高 2394px（2.7 屏）、平均行高 236px、
   每行 4.1 条理由全部平铺。现在：
     · 结论行 56px，单行省略 —— 10 行约 620px（0.7 屏）
     · 理由按「警告 / 事实 / 条件」分组，默认收起，点行展开
     · 原文一字未删，只改默认可见性与分组归属 */

// 风险等级 -> 展示样式与中文
const RISK_META = {
  high:   { label: '高', cls: 'b-red' },
  medium: { label: '中', cls: 'b-yellow' },
  low:    { label: '低', cls: 'b-pos' }
};
const RISK_FILTERS = {
  all:    () => true,
  high:   (it) => it.risk_level === 'high',
  medium: (it) => it.risk_level === 'medium',
  low:    (it) => it.risk_level === 'low'
};
const COND_LABEL = { price_band_bp: '价格带', size_usd: '规模', timing: '时机', route: '路由' };

let AFILTER = 'all';
const AOPEN = new Set();          // 记住哪些行是展开的（刷新数据后不丢状态）

function skeleton(n) {
  let s = '';
  for (let i = 0; i < n; i++) {
    s += '<tr class="arow"><td colspan="6"><span class="sk sk-row"></span></td></tr>';
  }
  return s;
}

// 只把**数字**包成高亮（不做整体正则替换：那会破坏 &amp;/&#39; 这类实体）
function numHi(s) {
  const parts = String(s === null || s === undefined ? '' : s).split(/(\d[\d,]*(?:\.\d+)?)/);
  return parts.map((p, i) => (i % 2 === 1)
    ? '<span class="ag-num">' + esc(p) + '</span>' : esc(p)).join('');
}

// 结论字段形如「不做（成本为正且腿风险高）」-> 主结论 + 括注分开
function splitVerdict(v) {
  const s = String(v === null || v === undefined ? '' : v).trim();
  const m = s.match(/^(.+?)[（(](.+)[）)]$/);
  return m ? { label: m[1].trim(), note: m[2].trim() } : { label: s || '—', note: '' };
}

function condChips(conds, max) {
  const ks = Object.keys(conds || {});
  const out = ks.slice(0, max).map((k) => {
    const raw = conds[k];
    const lab = COND_LABEL[k] || k;
    let val = (typeof raw === 'number')
      ? raw.toLocaleString('en-US', { maximumFractionDigits: 2 })
      : String(raw);
    if (String(val).length > 13) val = String(val).slice(0, 11) + '…';
    return '<span class="tag" title="' + esc(lab + '：' + String(raw)) + '">' +
      esc(lab) + ' <span class="ag-num">' + esc(val) + '</span></span>';
  });
  if (ks.length > max) out.push('<span class="tag">+' + (ks.length - max) + '</span>');
  return out.join('');
}

function assessDetailHTML(it) {
  const warnings = it.warnings || [];
  const facts = it.rationale || [];
  const conds = it.conditions || {};
  const v = splitVerdict(it.verdict);
  let h = '';
  if (v.note) {
    h += '<div class="ag"><div class="ag-head">结论依据</div>' +
         '<div class="ag-item">' + numHi(v.note) + '</div></div>';
  }
  h += '<div class="ag ag-warn"><div class="ag-head">' +
    '<span class="abadge b-red">红 ' + warnings.length + '</span>会改变结论</div>' +
    (warnings.length
      ? warnings.map((x) => '<div class="ag-item">' + numHi(x) + '</div>').join('')
      : '<div class="ag-item a-empty">无警告</div>') + '</div>';
  h += '<div class="ag"><div class="ag-head">' +
    '<span class="abadge">事实 ' + facts.length + '</span>中立背景，不改变结论</div>' +
    (facts.length
      ? facts.map((x) => '<div class="ag-item">' + numHi(x) + '</div>').join('')
      : '<div class="ag-item a-empty">无</div>') + '</div>';
  const ks = Object.keys(conds);
  if (ks.length) {
    h += '<div class="ag"><div class="ag-head">条件点位</div>' +
      ks.map((k) => '<div class="ag-item"><span class="ag-num">' +
        esc(COND_LABEL[k] || k) + '</span>　' + numHi(String(conds[k])) + '</div>').join('') +
      '</div>';
  }
  return h;
}

function renderAssess() {
  const tbody = $('assess-body');
  if (!tbody) return;
  const r = DATA.assess;
  if (!r) { tbody.innerHTML = skeleton(3); return; }
  if (r.available === false) {
    tbody.innerHTML = '<tr class="arow"><td colspan="6" class="a-empty">' +
      esc(r.error || '风险引擎暂不可用') + '</td></tr>';
    setText('assess-count', '');
    return;
  }
  const all = (r.items || []).filter((x) => !x.error);
  const items = all.filter(RISK_FILTERS[AFILTER] || RISK_FILTERS.all);
  const cnt = { high: 0, medium: 0, low: 0 };
  all.forEach((x) => { if (cnt[x.risk_level] !== undefined) cnt[x.risk_level]++; });
  setText('assess-count', all.length + ' 个标的 · 红 ' + cnt.high + ' / 黄 ' +
    cnt.medium + ' / 可做 ' + cnt.low);

  if (!items.length) {
    tbody.innerHTML = '<tr class="arow"><td colspan="6" class="a-empty">' +
      '当前筛选下没有标的</td></tr>';
    return;
  }

  tbody.innerHTML = items.map((it) => {
    const base = it.base;
    const risk = RISK_META[it.risk_level] || { label: '?', cls: 'b-red' };
    const v = splitVerdict(it.verdict);
    const warnings = it.warnings || [];
    const facts = it.rationale || [];
    const main = warnings.length ? warnings[0] : (facts.length ? facts[0] : '—');
    const conds = it.conditions || {};
    const condN = Object.keys(conds).length;
    const open = AOPEN.has(base);
    return '' +
      '<tr class="arow' + (open ? ' is-open' : '') + '" data-base="' + esc(base) +
        '" tabindex="0" role="button" aria-expanded="' + open + '">' +
        '<td class="base-name">' + esc(base) +
          (it.event_severity === 'block' ? ' <span class="tag thin">事件窗口</span>' : '') +
        '</td>' +
        '<td><span class="abadge ' + risk.cls + '">' + esc(risk.label) + '</span></td>' +
        '<td><span class="abadge" title="' + esc(v.note || v.label) + '">' +
          esc(v.label) + '</span></td>' +
        '<td class="sep"><span class="a-main" title="' + esc(main) + '">' +
          esc(main) + '</span></td>' +
        '<td class="sep"><span class="a-cond">' +
          (condN ? condChips(conds, 2) : '<span class="mono-dim">—</span>') +
        '</span></td>' +
        '<td class="chev-col"><span class="a-chev">›</span></td>' +
      '</tr>' +
      '<tr class="adetail" data-for="' + esc(base) + '"' + (open ? '' : ' hidden') + '>' +
        '<td colspan="6">' + assessDetailHTML(it) + '</td>' +
      '</tr>';
  }).join('');
}

function toggleAssessRow(tr) {
  const base = tr.dataset.base;
  if (!base) return;
  if (AOPEN.has(base)) AOPEN.delete(base); else AOPEN.add(base);
  renderAssess();
  const again = document.querySelector('#assess-body tr.arow[data-base="' +
    String(base).replace(/"/g, '') + '"]');
  if (again) again.focus();
}

function initAssessTable() {
  const tbody = $('assess-body');
  if (!tbody) return;
  tbody.addEventListener('click', (e) => {
    const tr = e.target.closest('tr.arow');
    if (tr && tr.dataset.base) toggleAssessRow(tr);
  });
  tbody.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const tr = e.target.closest('tr.arow');
    if (!tr || !tr.dataset.base) return;
    e.preventDefault();
    toggleAssessRow(tr);
  });

  const f = $('afilter');
  if (f) {
    f.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-f]');
      if (!b) return;
      AFILTER = b.dataset.f;
      f.querySelectorAll('button[data-f]').forEach((x) =>
        x.classList.toggle('active', x === b));
      renderAssess();
    });
  }

  const all = $('afoldall');
  if (all) {
    all.addEventListener('click', () => {
      const items = ((DATA.assess && DATA.assess.items) || []).filter((x) => !x.error)
        .filter(RISK_FILTERS[AFILTER] || RISK_FILTERS.all);
      const anyClosed = items.some((x) => !AOPEN.has(x.base));
      items.forEach((x) => {
        if (anyClosed) AOPEN.add(x.base); else AOPEN.delete(x.base);
      });
      all.textContent = anyClosed ? '全部收起' : '全部展开';
      renderAssess();
    });
  }
}

async function loadAssess() {
  try {
    DATA.assess = await api('/api/assess');
  } catch (e) {
    DATA.assess = { available: false, error: '风险引擎暂不可用', items: [] };
  }
  renderAssess();       // 无论当前在哪个视图都渲染：避免隐藏视图里残留骨架屏
}

/* ---------------- 右下角提醒：持仓期风控（黄 / 红两档） ----------------
   数据来自 /api/alerts（tools/position_watch.py 落盘的 alerts.json）。

   两条约定：
     ① **没有提醒就整块收起** —— 不设"绿色 / 一切正常"档（用户 2026-09-20 明确：不要绿）。
     ② 只有**黄/红**两种色位：红 = 必须立刻处理，黄 = 值得看。 */

// 告警文本里带 markdown 风格的 `**粗体**` 与 `` `代码` ``。
// ⚠️ 原样输出会在页面上出现**字面星号/反引号**（本项目已踩过 4 次，
//    前端探针 tools/ui_probe.js 至今仍在盯着这两个符号），
//    所以这里先整体转义、再把这两种标记**渲染成元素**。
function mdInline(s) {
  return esc(s === null || s === undefined ? '' : String(s))
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

// 用户点过"收起"的那一批巡检（按批次时间戳记）。出现**新批次**才会再次弹出，
// 否则每个刷新周期都弹一次会很烦。
let ALERT_DISMISS_TS = null;

function fmtAgo(iso) {
  const t = Date.parse(iso || '');
  if (isNaN(t)) return '';
  const min = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (min < 1) return '刚刚';
  if (min < 60) return min + ' 分钟前';
  const h = Math.round(min / 60);
  if (h < 24) return h + ' 小时前';
  return Math.round(h / 24) + ' 天前';
}

async function loadAlerts() {
  const dock = $('alert-dock');
  if (!dock) return;
  let r;
  try {
    r = await api('/api/alerts');
  } catch (e) {
    console.error(e);               // 可选功能不可用 -> 静默：不该拖死整页
    return;
  }
  const alerts = (r && r.alerts) || [];
  const pill = $('alert-pill');
  // ① 没有提醒（或还没有巡检结果）-> 整块收起，不占色位
  if (!r || r.available === false || !alerts.length) {
    dock.hidden = true;
    if (pill) pill.hidden = true;
    return;
  }
  // 顶栏那一枚常年可见的计数（弹窗可能被收起，这个不会）
  if (pill) {
    const ic0 = r.intensity_counts || {};
    pill.hidden = false;
    pill.textContent = '提醒 红 ' + (ic0.red || 0) + ' · 黄 ' + (ic0.yellow || 0);
    pill.className = 'alert-pill' + ((r.intensity === 'red') ? '' : ' yellow');
  }
  // ② 用户收起过且仍是同一批 -> 保持收起
  if (ALERT_DISMISS_TS !== null && ALERT_DISMISS_TS === r.ts) return;

  const lvl = (r.intensity === 'red') ? 'red' : 'yellow';
  dock.className = 'alert-dock lvl-' + lvl;
  dock.hidden = false;

  const ic = r.intensity_counts || {};
  $('alert-sum').textContent = '红 ' + (ic.red || 0) + ' · 黄 ' + (ic.yellow || 0) +
    (r.positions ? ' ｜ 持仓 ' + r.positions : '');

  $('alert-list').innerHTML = alerts.map((a) => {
    // 强度徽标：只有黄/红；无强度（info，只记录）用中性"记录"标
    const badge = a.intensity
      ? '<span class="alert-lvl lvl-' + a.intensity + '">' +
          esc(a.intensity_label || '') + '</span>'
      : '<span class="alert-lvl">记录</span>';
    return '<div class="alert-item">' +
      '<div class="alert-item-top">' + badge +
        '<span class="alert-base">' + esc(a.base) + '</span>' +
        '<span class="alert-what">' + mdInline(a.title) + '</span>' +
      '</div>' +
      (a.detail ? '<div class="alert-detail">' + mdInline(a.detail) + '</div>' : '') +
      (a.action ? '<div class="alert-action">→ ' + mdInline(a.action) + '</div>' : '') +
      '</div>';
  }).join('');

  const when = fmtAgo(r.ts);
  // ⚠️ 页面上**不写文件路径**（用户要求：前端不需要"文件所在位置"这种证据）。
  //    来源改成一句白话；真实路径仍在接口的 source_ref 里备查，只是不渲染。
  $('alert-foot').textContent =
    '巡检时间 ' + String(r.ts || '—').slice(0, 16).replace('T', ' ') +
    (when ? '（' + when + '）' : '') +
    ' ｜ ' + (r.source || '持仓期巡检') +
    ' ｜ 只有黄/红两档：没有提醒就不显示；只告警，不自动下单。';

  $('alert-close').onclick = function () {
    ALERT_DISMISS_TS = r.ts;
    dock.hidden = true;
  };
}

/* ================= 机会名单（首页置顶） =================
   判据与口径**全部来自后端** `/api/opportunities`：
   前端不写门槛数字、也不自己判断谁达标 —— 否则"页面上写的判据"
   和"代码里用的判据"迟早漂成两个数（本项目踩过同类坑，见 docs/48 与
   common/strategy_params.py 的注释）。这里只做渲染。 */

function fmtBp(v, d = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return (v > 0 ? '+' : '') + Number(v).toFixed(d);
}
function fmtPct(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const p = Number(v) * 100;
  return (p < 1 ? p.toFixed(3) : p.toFixed(2)) + '%';
}
function fmtUsd(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return Math.round(Number(v)).toLocaleString('en-US') + ' USD';
}

const OOPEN = new Set();          // 记住哪些行展开着（刷新数据后不丢状态）

/* ---- 「进场证据」-----------------------------------------------------
   用户在 2026-09-20 提的问题：**凭什么证明我能进场？小白怎么看得懂？**
   所以这块只做一件事：把后端已经算好的数字，翻成「数字 + 一句白话」。

   三个分组对应小白真正会问的三个问题：
     ① 能不能挂上   ② 划不划算   ③ 最大的坑在哪
   数字**全部来自后端**（items[].evidence / net_bp / size_note），
   前端不自己算权重、也不自己判定 —— 免得"页面说的"和"引擎算的"变成两套。 */

function evRow(k, v) {
  return '<dt>' + esc(k) + '</dt><dd>' + v + '</dd>';
}

function oppEvidenceHTML(r) {
  const ev = r.evidence;
  if (!ev) {
    return '<p class="opp-ev-miss">这一单的执行成本还没算出来（引擎暂不可用），' +
      '所以现在只能看到基差，看不到进场证据。</p>';
  }
  const h = ['<div class="opp-ev">'];

  h.push('<div class="opp-ev-h ok">① 能不能挂上</div><dl class="opp-ev-dl">');
  h.push(evRow('两边盘口都在', r.tradable
    ? '现货和永续都有报价 —— 缺任何一边，这一单根本下不出去'
    : '<span class="opp-warn">有一条腿的盘口是空的，这一单下不出去</span>'));
  h.push(evRow('挂单能不能省点差', ev.maker_allowed
    ? '现在正是所内撮合窗口 —— 挂单是「赚」点差，不是付点差'
    : '<span class="opp-warn">现在不是所内撮合窗口 —— 挂单也按吃单收费</span>'));
  h.push(evRow('挂单价参考', '现货 ±' + fmt(ev.half_s) + ' bp ｜ 永续 ±' +
    fmt(ev.half_p) + ' bp'));
  h.push(evRow('这份量能吃下', fmtUsd(r.depth_within_5bp_usd) +
    '（≤5bp 口径；再大就会被吃穿，规模只能这么大）'));
  h.push('</dl>');

  h.push('<div class="opp-ev-h">② 划不划算</div><dl class="opp-ev-dl">');
  h.push(evRow('预计执行成本', fmt(ev.best_cost) + ' bp（' + esc(ev.best_mode || '—') +
    '；含点差 / 手续费 / 冲击 / 逆向选择）'));
  h.push(evRow('基差 − 成本', fmtBp(r.basis_bp) + ' − ' + fmt(ev.best_cost) + ' = ' +
    '<b class="' + cls(r.net_bp) + '">净 ' + fmtBp(r.net_bp) + ' bp</b>' +
    (r.net_bp > 0 ? ' —— 空间是正的，这一单值得考虑'
                  : ' —— 空间是负的，这一单不该做')));
  h.push('</dl>');

  h.push('<div class="opp-ev-h warn">③ 最大的坑（这才是关键）</div><dl class="opp-ev-dl">');
  h.push(evRow('两条腿同时成交', '<span class="opp-pct">' + fmtPct(ev.p_both) + '</span>' +
    ' —— 绝大多数时候，你根本建不上完整仓位'));
  h.push(evRow('只成交一条腿', '<span class="opp-pct">' + fmtPct(ev.p_part) + '</span>' +
    ' —— 手上会剩一个没对冲的方向敞口，这是要防的事'));
  h.push(evRow('两条腿都没成交', fmtPct(ev.p_none)));
  h.push('</dl>');

  if (r.size_note) h.push('<p class="opp-ev-note">' + esc(r.size_note) + '</p>');
  if (r.top_warning) {
    h.push('<p class="opp-ev-note">引擎另有一条警告：' + esc(r.top_warning) + '</p>');
  }

  h.push('<p class="opp-ev-sum">一句话：这一单有 <b>净 ' + fmtBp(r.net_bp) + ' bp</b> 的空间，' +
    '但「两条腿同时成交」只有 <b>' + fmtPct(ev.p_both) + '</b>，而「只成交一条腿」有 <b>' +
    fmtPct(ev.p_part) + '</b> —— 真正要盯的不是基差有多大，' +
    '而是能不能两条腿一起成交。</p>');

  h.push('</div>');
  return h.join('');
}

function renderOpps() {
  const tb = $('opp-body');
  if (!tb) return;
  const o = DATA.opps;
  if (!o) { tb.innerHTML = '<tr><td colspan="6"><span class="sk sk-row"></span></td></tr>'; return; }

  if (!o.available) {
    setText('opp-hint', '暂不可用');
    tb.innerHTML = '<tr class="opp-empty"><td colspan="6">机会名单暂不可用：' +
      esc(o.error || '风险引擎未就绪') + '</td></tr>';
    setText('opp-criteria', '');
    setText('opp-note', '');
    return;
  }

  const th = o.threshold || {};
  const w = o.window || {};
  const sc = o.scan || {};
  setText('opp-hint', '入选门槛 ' + fmt(th.entry_thr_bp) + ' bp · ' +
    (w.in_house ? '所内撮合窗口（可挂单）' : '非所内窗口（' + (w.route || '?') + '）') +
    ' · 扫描 ' + (sc.total || 0) + ' 只');
  setText('opp-criteria', o.criteria || '');
  setText('opp-note', o.disclaimer || '');

  const items = o.items || [];
  if (!items.length) {
    // 空名单必须说清"为什么空"：是没到门槛，还是根本不在窗口里。
    let msg = '<strong>当前无标的达到开仓门槛</strong>（' + fmt(th.entry_thr_bp) + ' bp）';
    msg += w.in_house ? '。' : '；且当前不是所内撮合窗口，全部不参与筛选。';
    const c = o.closest;
    if (c) {
      msg += ' 最接近：<span class="opp-closest">' + esc(c.base) + ' ' +
        fmtBp(c.basis_bp) + ' bp</span>，还差 ' + fmt(c.gap_bp) + ' bp。';
    } else {
      msg += '（当前没有任何「基差为正且两腿齐全」的标的 —— 连接近的都还没有，' +
        '不是门槛设得太高。）';
    }
    tb.innerHTML = '<tr class="opp-empty"><td colspan="6">' + msg + '</td></tr>';
    return;
  }

  tb.innerHTML = items.map((r) => {
    const has = (r.depth_within_5bp_usd !== null && r.depth_within_5bp_usd !== undefined);
    // 「深度不足」用项目自己的 OB_THIN_USD 口径，不是前端另定的阈值
    const thin = has && r.thin_usd && r.depth_within_5bp_usd < r.thin_usd;
    const d5 = has ? Math.round(r.depth_within_5bp_usd).toLocaleString('en-US') : '—';
    const open = OOPEN.has(r.base);
    return '' +
      '<tr class="opp-hit opp-row' + (open ? ' is-open' : '') +
        '" data-base="' + esc(r.base) + '" tabindex="0" role="button" aria-expanded="' +
        open + '">' +
        '<td class="opp-base">' + esc(r.base) + '</td>' +
        '<td class="sep opp-basis ' + cls(r.basis_bp) + '">' + fmtBp(r.basis_bp) + '</td>' +
        '<td class="opp-margin ' + cls(r.margin_bp) + '">' + fmtBp(r.margin_bp) + '</td>' +
        '<td class="sep opp-depth' + (thin ? ' thin' : '') + '"' +
          (thin ? ' title="低于项目自己的深度门槛 ' + Math.round(r.thin_usd) +
                  ' USD —— 能不能做还要看深度，不只是基差"' : '') + '>' +
          d5 + (thin ? '<span class="opp-tag">深度不足</span>' : '') + '</td>' +
        '<td class="sep opp-verdict">' + esc(r.verdict || '—') + '</td>' +
        '<td class="chev-col"><span class="a-chev">›</span></td>' +
      '</tr>' +
      '<tr class="adetail" data-for="' + esc(r.base) + '"' + (open ? '' : ' hidden') + '>' +
        '<td colspan="6">' + oppEvidenceHTML(r) + '</td>' +
      '</tr>';
  }).join('');
}

function toggleOppRow(tr) {
  const base = tr.dataset.base;
  if (!base) return;
  if (OOPEN.has(base)) OOPEN.delete(base); else OOPEN.add(base);
  renderOpps();
  const again = document.querySelector('#opp-body tr.opp-row[data-base="' +
    String(base).replace(/"/g, '') + '"]');
  if (again) again.focus();
}

function initOppsTable() {
  const tb = $('opp-body');
  if (!tb) return;
  tb.addEventListener('click', (e) => {
    const tr = e.target.closest('tr.opp-row');
    if (tr && tr.dataset.base) toggleOppRow(tr);
  });
  tb.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const tr = e.target.closest('tr.opp-row');
    if (!tr || !tr.dataset.base) return;
    e.preventDefault();                        // 空格不要滚页面
    toggleOppRow(tr);
  });
}

async function loadOpps() {
  try {
    DATA.opps = await api('/api/opportunities');
  } catch (e) {
    DATA.opps = { available: false, error: '接口不可用', items: [] };
    console.error(e);
  }
  renderOpps();
}

/* ---------------- 渲染总入口 ----------------
   三个视图的表格都是 10 行量级，重绘成本可忽略（<1ms），所以**全部渲染**：
   这样隐藏视图里不会残留骨架屏，切过去一定是现成的内容。
   真正的省法在别处 —— DATA 缓存让切换零网络等待、且不会重复请求接口。 */
function renderAll() {
  if (DATA.overview) {
    updateKPI(DATA.overview);
    renderPairs(DATA.overview);
  }
  if (DATA.sessions) renderSessions(DATA.sessions);
  if (DATA.status) {
    updateFreshness(DATA.status);
    renderStatus(DATA.status);
  }
  renderOpps();
  renderAssess();
  syncFoldAll();
}

/* 取数：**一个接口失败不许拖死另外两个**。
   ⚠️ 实测踩到（2026-09-20 全量自检偶发变红，根因就是这里）：
     refresh() 原本用 Promise.all(三个接口)，任意一个**瞬时**失败 -> 整组 reject
     -> DATA.overview/sessions/status 全为 null -> renderAll() 什么都不画
     -> **骨架屏永远留在页面上**；而真浏览器验收的等待条件是"页面里没有 .sk"，
     于是浏览器等到超时、报"浏览器没跑出结果" —— **接口是偶发的，表现却是页面卡死**。
   这条最阴的地方在于：HTTP 通、JS 不报错，只有"永远转圈的骨架屏"。
   改成逐个取、谁到了渲染谁。 */
async function fetchOne(path) {
  try {
    return await api(path);
  } catch (e) {
    console.error(e);
    return null;
  }
}

let REFRESH_RETRY = 0;

async function refresh() {
  const [ov, ses, st] = await Promise.all([
    fetchOne('/api/overview'),
    fetchOne('/api/session-compare'),
    fetchOne('/api/data-status'),
  ]);
  if (ov) DATA.overview = ov;
  if (ses) DATA.sessions = ses;
  if (st) DATA.status = st;
  renderAll();

  if (ov && ses && st) {
    REFRESH_RETRY = 0;
    $('live-dot').className = 'dot on';
    return;
  }
  // 有缺的：点亮错误灯，并**补一次**（最多 3 次，别无限打接口）
  $('live-dot').className = 'dot err';
  if (REFRESH_RETRY < 3) {
    REFRESH_RETRY += 1;
    setTimeout(refresh, 3000);
  }
}

/* 兜底扫描：万一某个接口长期不可用，骨架屏不能永远转下去 ——
   25 秒后把残留的 .sk 换成一句能看懂的话。
   这条同时消灭"卡住的占位符"这一类问题（探针的 stuck_loading 语义）。 */
function sweepSkeletons() {
  const left = document.querySelectorAll('.sk');
  left.forEach((el) => {
    const txt = document.createElement('span');
    txt.className = 'sk-fallback';
    txt.textContent = '暂时取不到数据 —— 请确认本地服务在运行';
    el.replaceWith(txt);
  });
  return left.length;
}

async function loadTimeline() {
  try {
    TL = await api('/api/timeline');
    const bases = [...new Set(TL.flatMap((p) => Object.keys(p.basis)))].sort();
    renderToolbar(bases);
    drawChart();
  } catch (e) { console.error(e); }
}

async function boot() {
  initTabs();
  initFolds();
  initAssessTable();
  initOppsTable();
  applyView(currentView(), { animate: false, scroll: false });   // 先定视图，再取数

  const pill = $('alert-pill');
  if (pill) pill.addEventListener('click', () => go('decision'));

  await loadHealth();
  await refresh();
  await loadTimeline();
  // 机会名单随主循环刷新（它的判据依赖实时基差，必须跟着走）
  loadOpps();
  // 风险与理由单独加载：它不可用时**不影响**上面的策略视图（可选功能不该拖死整页）
  loadAssess();
  // 右下角提醒同理：没有持仓单/告警文件时静默不显示
  loadAlerts();

  tickClock();
  setInterval(tickClock, 1000);            // 时钟每秒走字（纯前端）
  setInterval(refresh, REFRESH_MS);
  setInterval(loadTimeline, REFRESH_MS * 3);
  setInterval(loadAssess, REFRESH_MS * 3);
  setInterval(loadOpps, REFRESH_MS);
  setInterval(loadAlerts, REFRESH_MS);
  setInterval(loadHealth, 30000);
  // 兜底：25 秒后清掉任何残留骨架屏（不让页面永远转圈）
  setTimeout(sweepSkeletons, 25000);
}

window.addEventListener('hashchange', () => applyView(currentView()));
boot();
