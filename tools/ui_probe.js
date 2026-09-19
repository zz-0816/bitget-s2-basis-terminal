/* 页面体检探针：把"看着对不对"变成"测出来是不是"。
 *
 * 检查项都对应真实踩过的坑：
 *   · literal_bold / literal_code —— 标题与表格用 esc() 原样输出，
 *     带 ** / 反引号的字符串会显示成字面符号（已修过 4 处）
 *   · clipped —— overflow:hidden 把文字切掉，肉眼容易忽略
 *   · offscreen —— 元素右缘超出视口（项目一那张表就是这样丢掉了一整列）
 *   · stuck —— 一直停在"加载中…"的占位符
 *   · basis —— 决策基准时间是否显式写在页面上（离线演示必须写）
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
  return {
    literal_bold: bold,
    literal_code: code,
    clipped: clipped.slice(0, 6),
    offscreen_right: Array.from(new Set(off)).slice(0, 6),
    stuck_loading: stuck,
    basis: basisEl ? basisEl.textContent : "(无 basis-ts 元素)",
    doc_w: document.documentElement.scrollWidth,
    viewport: vw,
    overflow_x: document.documentElement.scrollWidth > vw + 2
  };
})()
