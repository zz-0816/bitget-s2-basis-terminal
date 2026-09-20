/* 页面体检探针：把"看着对不对"变成"测出来是不是"。
 *
 * 检查项都对应真实踩过的坑：
 *   · literal_bold / literal_code —— 标题与表格用 esc() 原样输出，
 *     带 ** / 反引号的字符串会显示成字面符号（已修过 4 处）
 *   · clipped —— overflow:hidden 把文字切掉，肉眼容易忽略
 *   · offscreen —— 元素右缘超出视口（项目一那张表就是这样丢掉了一整列）
 *   · stuck —— 一直停在"加载中…"的占位符
 *   · basis —— 决策基准时间是否显式写在页面上（离线演示必须写）
 *
 * 2026-09-20 改版（docs/48）新增：
 *   · assess_* —— 决策表默认行高必须 ≤ 64px（改版前实测 236px 的"文字墙"），
 *     展开后单元格不得越出行边界
 *   · *_expanded —— **把所有折叠展开**再测一遍字面符号/横向溢出/表格宽度，
 *     因为 innerText 不含 display:none 的内容，不展开就测不到折叠里的坑
 */
(function () {
  var txt = document.body.innerText || "";
  var bold = txt.split("**").length - 1;
  var code = txt.split("`").length - 1;

  var clipped = [];
  document.querySelectorAll("*").forEach(function (el) {
    var cs = getComputedStyle(el);
    if ((cs.overflow === "hidden" || cs.overflowY === "hidden") &&
        el.scrollHeight > el.clientHeight + 3 && el.clientHeight > 0 &&
        el.children.length === 0) {
      clipped.push(el.tagName.toLowerCase() + "." +
        String(el.className || "").split(" ")[0] + "(-" +
        (el.scrollHeight - el.clientHeight) + "px)");
    }
  });

  var vw = window.innerWidth;
  var off = [];
  document.querySelectorAll("table, .panel, section, .table-wrap").forEach(function (el) {
    var r = el.getBoundingClientRect();
    if (r.right > vw + 2 && el.offsetParent !== null) {
      off.push(el.tagName.toLowerCase() + "." +
        String(el.className || "").split(" ")[0] + "(+" +
        Math.round(r.right - vw) + "px)");
    }
  });

  var stuck = [];
  document.querySelectorAll(".loading, .empty").forEach(function (el) {
    var s = (el.innerText || "").trim();
    if (/加载中|读取中/.test(s)) {
      stuck.push(s.slice(0, 22));
    }
  });

  var basisEl = document.getElementById("basis-ts");

  /* ---- 表格：默认行高 / 展开后的越界与宽度稳定性 ----
     ⚠️ 两张表用**同一套红线**：决策表（#assess-table）与机会名单（#opp-table）。
     机会名单也会展开（点行看「进场证据」），所以它同样要过行高与越界检查 ——
     只盯着一张表，另一张就会悄悄退化。 */
  function rowStats(tblId, rowSel) {
    var t = document.getElementById(tblId);
    var n = 0, maxH = 0;
    if (!t) return { n: 0, maxH: 0, w: null };
    Array.prototype.forEach.call(t.querySelectorAll("tbody " + rowSel), function (tr) {
      if (tr.offsetParent === null) return;                 // 隐藏视图里的不量
      if (tr.querySelector(".sk")) return;                  // 骨架屏不算
      n++;
      var h = tr.getBoundingClientRect().height;
      if (h > maxH) maxH = h;
    });
    return { n: n, maxH: maxH, w: Math.round(t.getBoundingClientRect().width) };
  }

  var at = document.getElementById("assess-table");
  var ot = document.getElementById("opp-table");
  var A = rowStats("assess-table", "tr.arow");
  var O = rowStats("opp-table", "tr.opp-row");

  /* 展开后的越界 / 截断：两张表都算 */
  function detailProblems(tblId) {
    var t = document.getElementById(tblId);
    var over = 0, cut = 0;
    if (!t) return { over: 0, cut: 0 };
    Array.prototype.forEach.call(t.querySelectorAll("tbody tr.adetail"), function (tr) {
      var td = tr.querySelector("td");
      if (!td) return;
      var a = tr.getBoundingClientRect(), b = td.getBoundingClientRect();
      if (b.bottom > a.bottom + 1 || b.right > a.right + 1) over++;
    });
    Array.prototype.forEach.call(t.querySelectorAll("tbody tr.adetail td"), function (td) {
      if (td.clientHeight > 0 && td.scrollHeight > td.clientHeight + 3) cut++;
    });
    return { over: over, cut: cut };
  }

  /* 把所有折叠/展开行临时打开 -> 在"最坏情况"下量一遍，然后**原样还原** */
  function expanded(fn) {
    var folds = Array.prototype.slice.call(document.querySelectorAll("details.fold"));
    var dets = Array.prototype.slice.call(document.querySelectorAll("tr.adetail"));
    var s1 = folds.map(function (d) { return d.open; });
    var s2 = dets.map(function (d) { return d.hidden; });
    folds.forEach(function (d) { d.open = true; });
    dets.forEach(function (d) { d.hidden = false; });
    var out = fn();
    folds.forEach(function (d, i) { d.open = s1[i]; });
    dets.forEach(function (d, i) { d.hidden = s2[i]; });
    return out;
  }

  var exp = expanded(function () {
    var t = document.body.innerText || "";
    var pa = detailProblems("assess-table");
    var po = detailProblems("opp-table");
    return {
      literal_bold_expanded: t.split("**").length - 1,
      literal_code_expanded: t.split("`").length - 1,
      doc_w_expanded: document.documentElement.scrollWidth,
      overflow_x_expanded: document.documentElement.scrollWidth > window.innerWidth + 2,
      table_w_expanded: at ? Math.round(at.getBoundingClientRect().width) : null,
      detail_overflow: pa.over,
      detail_clipped: pa.cut,
      opp_table_w_expanded: ot ? Math.round(ot.getBoundingClientRect().width) : null,
      opp_detail_overflow: po.over,
      opp_detail_clipped: po.cut
    };
  });

  var out = {
    literal_bold: bold,
    literal_code: code,
    clipped: clipped.slice(0, 6),
    offscreen_right: Array.from(new Set(off)).slice(0, 6),
    stuck_loading: stuck,
    basis: basisEl ? basisEl.textContent : "(无 basis-ts 元素)",
    doc_w: document.documentElement.scrollWidth,
    viewport: vw,
    overflow_x: document.documentElement.scrollWidth > vw + 2,
    assess_rows: A.n,
    assess_max_row_h: Math.round(A.maxH),
    table_w_collapsed: A.w,
    opp_rows: O.n,
    opp_max_row_h: Math.round(O.maxH),
    opp_table_w_collapsed: O.w
  };
  Object.keys(exp).forEach(function (k) { out[k] = exp[k]; });
  return out;
})()
