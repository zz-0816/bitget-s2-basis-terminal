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
  // 时间显示交给独立时钟（tickClock，每秒一次），不依赖健康检查的 30 秒节奏
  CLOCK_OFFSET_MS = new Date(h.server_time_utc).getTime() - Date.now();
  $('live-dot').className = 'dot on';

  $('window-title').textContent = closed
    ? '当前处于休市窗口 —— 这正是策略的建仓窗口'
    : '当前美股开市中 —— 休市窗口尚未开始';
  $('window-body').textContent = closed
    ? '美股已休市，rToken 与美股永续仍在 7×24 交易。休市时段现货点差显著放大（这是策略的收益来源），而基差通常在开盘时收敛（这是退出时机）。下方「分时段点差」量化放大倍数。'
    : '美股开市中，现货点差处于全天最窄区间——这是计算「休市窗口放大倍数」的基准值。策略的实际建仓窗口在每个交易日的 20:00 ET 之后及整个周末。';
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
  const bases = rows.map((r) => Math.abs(r.basis_bp || 0));
  const maxAbs = Math.max(...bases, 1e-9);

  tbody.innerHTML = rows.map((r) => {
    const s = r.spot, p = r.perp;
    const sBp = s ? s.spread_bp : null;
    const pBp = p ? p.spread_bp : null;
    const basis = r.basis_bp;
    const hot = Math.abs(basis || 0) >= maxAbs * 0.98;
    const ratio = (sBp && pBp) ? (sBp / Math.max(pBp, 1e-9)) : null;
    // 容量告警：永续腿顶部深度过小则标红（这是比点差更硬的约束）
    const depth = r.capacity ? r.capacity.perp_top_depth_usd : null;
    const thin = depth !== null && depth < 5000;
    return '<tr class="' + (hot ? 'best' : '') + '">' +
      '<td class="base-name">' + esc(r.base) +
        (hot ? ' <span class="tag hot">基差最大</span>' : '') +
        (thin ? ' <span class="tag thin">深度不足</span>' : '') + '</td>' +
      '<td>' + fmt(s && s.mid) + '</td>' +
      '<td class="sep ' + (sBp > 10 ? 'neg' : '') + '">' + fmt(sBp) +
        (ratio ? ' <span class="mono-dim">(' + ratio.toFixed(1) + '×)</span>' : '') + '</td>' +
      '<td>' + fmt(p && p.mid) + '</td>' +
      '<td class="sep">' + fmt(pBp) + '</td>' +
      '<td class="sep ' + cls(basis) + '"><strong>' + fmt(basis) + '</strong></td>' +
      '<td>' + (basis === null ? '—'
        : '<span class="tag">' + (basis < 0 ? '多现货 / 空永续' : '空现货 / 多永续') + '</span>') + '</td>' +
      '<td>' + (r.capacity ? fmt(r.capacity.perp_top_depth_usd, 0) : '—') + '</td>' +
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
  tickClock();
  setInterval(tickClock, 1000);            // 时钟每秒走字（纯前端）
  setInterval(refresh, REFRESH_MS);
  setInterval(loadTimeline, REFRESH_MS * 3);
  setInterval(loadHealth, 30000);
}
boot();
