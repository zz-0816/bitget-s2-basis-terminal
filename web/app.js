/* Basis Terminal —— 前端逻辑
   只消费 /api/*；无外部依赖（图表为手写 SVG，离线可用）。 */

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

  $('window-title').textContent = h.maker_benefit
    ? '当前处于「所内撮合」窗口 —— 这是策略唯一可交易的时段'
    : (closed ? '美股休市，但走 StockRoute —— 挂单也按 Taker 计费'
              : '美股开市中 —— 走 StockRoute，挂单省不了点差');
  $('window-body').textContent = h.maker_benefit
    ? '周末/节假日窗口：平台启用所内撮合，区分 Maker/Taker。现货腿挂单可「赚」半幅点差 —— 这是策略的收益来源。'
    : '常规交易时段：平台走 StockRoute 直连美股，所有订单按 Taker 计费（不分挂单/吃单）。此时段不宜做市，数据仅作对照基准。';
  $('footer-meta').textContent =
    '刷新间隔 ' + REFRESH_MS / 1000 + 's · 行情缓存 ' + h.tick_seconds + 's · 配对 ' + h.pairs + ' 组';
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

/* ---------------- 配对表 ---------------- */

function renderPairs(rows) {
  const tbody = document.querySelector('#pairs-table tbody');
  if (!rows || !rows.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">暂无数据</td></tr>';
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

  $('status-body').innerHTML =
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
  $('chart-hint').textContent = mode + ' · ' + TL.length + ' 个采样点';
}

/* ---------------- 主循环（固定节奏，无自检轮询） ---------------- */

/* ---------------- 执行决策：风险与理由（项目二） ---------------- */

// 风险等级 -> 展示样式与中文
const RISK_META = {
  high:   { label: '高', cls: 'neg' },
  medium: { label: '中', cls: '' },
  low:    { label: '低', cls: 'pos' }
};

async function loadAssess() {
  const body = document.getElementById('assess-body');
  const note = document.getElementById('assess-note');
  if (!body) return;
  let r;
  try {
    r = await api('/api/assess');
  } catch (e) {
    body.innerHTML = '<tr><td colspan="5">风险引擎暂不可用</td></tr>';
    return;
  }
  if (!r || r.available === false) {
    // 刻意不让"可选功能不可用"把整页拖死
    body.innerHTML = '<tr><td colspan="5">' +
      esc((r && r.error) || '风险引擎暂不可用') + '</td></tr>';
    if (note) note.textContent = '';
    return;
  }
  body.innerHTML = (r.items || []).map(function (it) {
    if (it.error) {
      return '<tr><td>' + esc(it.base) + '</td><td colspan="4">' + esc(it.error) + '</td></tr>';
    }
    const rm = RISK_META[it.risk_level] || { label: '?', cls: '' };
    const reasons = (it.rationale || []).map(function (x) {
      return '<div class="r-line">· ' + esc(x) + '</div>';
    }).join('');
    const warns = (it.warnings || []).map(function (x) {
      return '<div class="r-line r-warn">! ' + esc(x) + '</div>';
    }).join('');
    const cond = Object.keys(it.conditions || {}).map(function (k) {
      return esc(k) + '=' + esc(String(it.conditions[k]));
    }).join('；') || '—';
    return '<tr>' +
      '<td class="base-name">' + esc(it.base) +
        (it.event_severity === 'block'
          ? ' <span class="tag thin">事件窗口</span>' : '') + '</td>' +
      '<td class="' + rm.cls + '"><strong>' + esc(rm.label) + '</strong></td>' +
      '<td>' + esc(it.verdict) + '</td>' +
      '<td class="sep r-cell">' + reasons + warns + '</td>' +
      '<td class="sep mono-dim">' + cond + '</td>' +
      '</tr>';
  }).join('');
  if (note) {
    const nSrc = (r.items || []).filter(function (x) { return !x.source_count; }).length;
    note.textContent = (r.disclaimer || '') +
      (nSrc ? '  ｜ ' + nSrc + ' 个标的无可回溯事件来源，其事件判断的置信度已被压到 0.40。' : '');
  }
}

async function refresh() {
  try {
    const [ov, ses, st] = await Promise.all([
      api('/api/overview'), api('/api/session-compare'), api('/api/data-status'),
    ]);
    renderPairs(ov);
    renderSessions(ses);
    renderStatus(st);

    const basisVals = ov.map((r) => r.basis_bp).filter((v) => typeof v === 'number');
    const spotVals = ov.map((r) => r.spot && r.spot.spread_bp).filter((v) => typeof v === 'number');
    const perpVals = ov.map((r) => r.perp && r.perp.spread_bp).filter((v) => typeof v === 'number');
    const med = (a) => {
      if (!a.length) return null;
      const s = [...a].sort((p, q) => p - q);
      return s[Math.floor(s.length / 2)];
    };
    $('m-pairs').textContent = ov.length;
    $('m-basis').textContent = fmt(med(basisVals));
    $('m-spot').textContent = fmt(med(spotVals));
    $('m-perp').textContent = fmt(med(perpVals));
  } catch (e) {
    $('live-dot').className = 'dot err';
    console.error(e);
  }
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
  await loadHealth();
  await refresh();
  await loadTimeline();
  // 风险与理由单独加载：它不可用时**不影响**上面的策略视图（可选功能不该拖死整页）
  loadAssess();
  tickClock();
  setInterval(tickClock, 1000);            // 时钟每秒走字（纯前端）
  setInterval(refresh, REFRESH_MS);
  setInterval(loadTimeline, REFRESH_MS * 3);
  setInterval(loadHealth, 30000);
}
boot();
