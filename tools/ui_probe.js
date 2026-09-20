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

  /* ---- 小白三问（默认视图）：三张卡的结论有没有真的渲染出来 ----
     为什么单独探这一块：这一页的价值全在"结论有没有出来"。
     接口挂了或渲染漏了时，页面**看起来仍然是正常的** —— 有标题、有边框、
     有三张卡，只是徽标停在占位符"—"上。这种失败肉眼最容易放过。 */
  var sigCards = document.querySelectorAll(".sig-card");
  var sigBadgeIds = ["sig-buy-badge", "sig-sell-badge", "sig-risk-badge"];
  var sigBadges = sigBadgeIds.map(function (id) {
    var el = document.getElementById(id);
    return el ? (el.textContent || "").trim() : "(缺元素)";
  });
  var sigHead = document.getElementById("sig-headline");
  var sigHeadTxt = sigHead ? (sigHead.textContent || "").trim() : "";
  var sigChecks = document.querySelectorAll("#sig-buy-checks .sig-chk");
  var sigStateList = Array.prototype.map.call(sigCards, function (el) {
    return el.dataset.state || "";
  });

  /* ---- 测算金额输入（用户可填自己的金额）----
     这里只报**静态事实**；"改金额 -> 结果跟着变"的交互由
     `tools/reproduce_check.py` 的接口契约检查覆盖（那边能直接比两次响应）。 */
  var sizeInput = document.getElementById("size-input");
  var sizeNoteEl = document.getElementById("sig-size-note");
  var sizeScopeEl = document.getElementById("sig-size-scope");
  var sizeNote = sizeNoteEl && !sizeNoteEl.hidden
    ? (sizeNoteEl.textContent || "").trim() : "";

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
    opp_table_w_collapsed: O.w,
    sig_cards: sigCards.length,
    sig_checks: sigChecks.length,
    sig_badges: sigBadges,
    sig_badges_blank: sigBadges.filter(function (t) {
      return !t || t === "—" || t === "(缺元素)";
    }).length,
    sig_states: sigStateList,
    sig_headline: sigHeadTxt.slice(0, 120),
    sig_headline_blank: !sigHeadTxt || !!(sigHead && sigHead.querySelector(".sk")),
    size_input_present: !!sizeInput,
    size_input_value: sizeInput ? String(sizeInput.value) : null,
    size_note: sizeNote,
    size_scope_len: sizeScopeEl ? (sizeScopeEl.textContent || "").trim().length : 0
  };
  Object.keys(exp).forEach(function (k) { out[k] = exp[k]; });
  return out;
})()
