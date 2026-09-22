#!/usr/bin/env node
/*
 * ui_shot.js —— 无头浏览器截图 + 页面交互验收（零依赖，只用 Node 内置能力）
 * ============================================================================
 *
 * 为什么需要它：
 *   "优化前端 UI" 如果只读 HTML/CSS 源码，那是**猜**。真正的判据是页面渲染出来
 *   长什么样、点了按钮之后有没有东西出来、控制台有没有报错。本项目已有
 *   `web_render_check.js`（最小 DOM 里跑 JS），但它**不做布局、不取真实网络、
 *   不会截图** —— 看不到"字叠在一起""表格撑破屏幕""点完没反应"这类问题。
 *
 * 本工具直接驱动本机已装的 Chrome/Edge（通过 CDP）：
 *   · 真加载页面、真发请求、真跑 JS
 *   · 可选：先点某个元素、等某个条件成立，再截图（"跑完整决策链"必须这样测）
 *   · 收集 console 报错与失败请求 —— 页面"看着正常但控制台一堆错"很常见
 *   · 截整页（captureBeyondViewport），不用手工拼接
 *
 * 为什么不用 puppeteer：沙箱里不能装依赖，而 Node 22+ 自带 WebSocket，
 * 直接用 CDP 就够，不引入任何 npm 包（评委 clone 下来也能跑）。
 *
 * 用法：
 *   node tools/ui_shot.js --url http://127.0.0.1:8788/ --out shot.png
 *   node tools/ui_shot.js --url http://127.0.0.1:8788/ --click "#run" \
 *        --until "document.querySelectorAll('#stages .stage').length>0" \
 *        --out chain.png --width 1680
 *
 * 退出码：0 = 页面无 console 报错且（若给了 --until）条件成立；1 = 否则。
 */

const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

// ---------------------------------------------------------------- 参数
function parseArgs(argv) {
  const a = { width: 1680, height: 1000, wait: 800, timeout: 45000, settle: 400,
              click: null, until: null, out: null, url: null, browser: null,
              keepNoise: false, full: true, scroll: 0, eval: null, evalFile: null,
              json: null };
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i];
    const v = argv[i + 1];
    if (k === "--url") a.url = v, i++;
    else if (k === "--out") a.out = v, i++;
    else if (k === "--eval") a.eval = v, i++;
    else if (k === "--eval-file") a.evalFile = v, i++;
    else if (k === "--json") a.json = v, i++;
    else if (k === "--click") a.click = v, i++;
    else if (k === "--until") a.until = v, i++;
    else if (k === "--width") a.width = +v, i++;
    else if (k === "--height") a.height = +v, i++;
    else if (k === "--wait") a.wait = +v, i++;
    else if (k === "--timeout") a.timeout = +v, i++;
    else if (k === "--settle") a.settle = +v, i++;
    else if (k === "--scroll") a.scroll = +v, i++;
    // --viewport：只截视口（不截整页）。整页图太长，缩略后看不清字；
    //            要看"排版到底怎么样"必须用视口图 + --scroll 分段看。
    else if (k === "--viewport") a.full = false;
    else if (k === "--browser") a.browser = v, i++;
    else if (k === "--keep-noise") a.keepNoise = true;
    else if (k === "-h" || k === "--help") { console.log(fs.readFileSync(__filename, "utf8").split("*/")[0]); process.exit(0); }
  }
  if (!a.url || !a.out) { console.error("需要 --url 与 --out"); process.exit(2); }
  return a;
}

// ---------------------------------------------------------------- 浏览器定位
function findBrowser(explicit) {
  if (explicit) return explicit;
  const cands = [
    process.env.CHROME_PATH,
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    path.join(process.env.LOCALAPPDATA || "", "Google\\Chrome\\Application\\chrome.exe"),
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ];
  for (const c of cands) { try { if (c && fs.existsSync(c)) return c; } catch (_) {} }
  return null;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------- CDP 极简客户端
class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.handlers = []; }
  static async attach(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => {
      ws.addEventListener("open", res, { once: true });
      ws.addEventListener("error", (e) => rej(new Error("ws error: " + (e.message || "?"))), { once: true });
    });
    const c = new CDP(ws);
    ws.addEventListener("message", (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (m.id && c.pending.has(m.id)) {
        const { res, rej } = c.pending.get(m.id); c.pending.delete(m.id);
        m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
      } else if (m.method) { c.handlers.forEach((h) => h(m)); }
    });
    return c;
  }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((res, rej) => this.pending.set(id, { res, rej }));
  }
  on(fn) { this.handlers.push(fn); }
}

async function fetchJSON(url, tries = 60) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(url);
      if (r.ok) return await r.json();
    } catch (_) {}
    await sleep(250);
  }
  throw new Error("无法连接调试端口：" + url);
}

// ---------------------------------------------------------------- 主流程
async function main() {
  const args = parseArgs(process.argv);
  const bin = findBrowser(args.browser);
  if (!bin) { console.error("[FAIL] 找不到 Chrome/Edge；可用 --browser 显式指定"); process.exit(1); }

  // ---- 硬看门狗：**保证这个脚本一定会退出** ----
  // 为什么必须有（2026-09-21 实测踩到）：
  //   全量自检里的前端验收报"浏览器超过 300 秒没返回（decision）"。
  //   `--until` 的轮询本身是有上限的（args.timeout），所以 300 秒不是等条件等出来的 ——
  //   是**收尾阶段挂住了**（最可能：Chrome 无响应，`browser.close()` 不返回），
  //   于是这个脚本永不退出，把所有调用它的东西一起拖死。
  //   同一个道理和前端那句一样：**工具不能依赖"外部一定会响应"**。
  //   上限取 args.timeout + 90 秒（留足启动与截图），到点就退出并说明原因。
  const hardLimit = args.timeout + 90000;
  const watchdog = setTimeout(() => {
    console.log("  [!! ] 硬看门狗触发：整个流程超过 " + Math.round(hardLimit / 1000)
      + " 秒仍未结束 —— 大概率是 Chrome 无响应。"
      + "（退出码 2；截图/JSON 可能没生成）");
    process.exit(2);
  }, hardLimit);
  if (watchdog.unref) watchdog.unref();

  const port = 9200 + Math.floor(Math.random() * 500);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "ui-shot-"));
  const child = spawn(bin, [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--hide-scrollbars", "--force-device-scale-factor=1",
    `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
    `--window-size=${args.width},${args.height}`,
    "about:blank",
  ], { stdio: "ignore" });

  let cdp = null;
  const cleanup = () => { try { cdp && cdp.ws.close(); } catch (_) {} try { child.kill(); } catch (_) {} };
  process.on("exit", cleanup);

  try {
    const ver = await fetchJSON(`http://127.0.0.1:${port}/json/version`);
    cdp = await CDP.attach(ver.webSocketDebuggerUrl);

    const targets = await fetchJSON(`http://127.0.0.1:${port}/json/list`);
    const page = targets.find((t) => t.type === "page");
    if (!page) throw new Error("没有 page target");
    const p = await CDP.attach(page.webSocketDebuggerUrl);

    // ---- 收集 console 报错 / 失败请求 / 页面异常 ----
    const errors = [], failed = [];
    let probeValue = null;
    p.on((m) => {
      if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
        errors.push((m.params.args || []).map((a) => a.value ?? a.description ?? a.type).join(" ").slice(0, 200));
      }
      if (m.method === "Runtime.exceptionThrown") {
        const d = m.params.exceptionDetails || {};
        errors.push(("JS 异常: " + (d.exception && (d.exception.description || d.exception.value) || d.text || "")).slice(0, 200));
      }
      if (m.method === "Network.loadingFailed") {
        failed.push(`${m.params.type || "?"} ${m.params.errorText || "?"}`);
      }
    });
    await p.send("Runtime.enable");
    await p.send("Network.enable");
    await p.send("Page.enable");
    await p.send("Emulation.setDeviceMetricsOverride",
                 { width: args.width, height: args.height, deviceScaleFactor: 1, mobile: false });

    await p.send("Page.navigate", { url: args.url });
    await sleep(args.wait);

    const evalJS = async (expr) => {
      const r = await p.send("Runtime.evaluate",
        { expression: expr, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw new Error(r.exceptionDetails.text || "evaluate 失败");
      return r.result && r.result.value;
    };

    // ---- 可选：点击某元素 ----
    if (args.click) {
      const okClick = await evalJS(`(() => {
        const el = document.querySelector(${JSON.stringify(args.click)});
        if (!el) return false;
        el.scrollIntoView({block:'center'}); el.click(); return true;
      })()`);
      console.log(okClick ? `  [OK ] 点击 ${args.click}` : `  [!! ] 找不到 ${args.click}`);
      if (!okClick) { process.exitCode = 1; }
      await sleep(args.settle);
    }

    // ---- 可选：轮询等待条件 ----
    let untilOk = null;
    if (args.until) {
      const t0 = Date.now();
      while (Date.now() - t0 < args.timeout) {
        try { if (await evalJS(args.until)) { untilOk = true; break; } } catch (_) {}
        await sleep(300);
      }
      if (untilOk === null) untilOk = false;
      console.log(untilOk ? `  [OK ] 条件成立：${args.until}`
                          : `  [!! ] 超时（${args.timeout}ms）未满足：${args.until}`);
      if (!untilOk) process.exitCode = 1;
      await sleep(400);   // 让最后一次渲染落定
    }

    // ---- 可选：打印任意表达式的结果（用来看 DOM 的真实状态，而不是靠眼睛猜）----
    // --eval-file 优先：表达式里有正则/反引号时，命令行传参会与 PowerShell 的
    // 引号规则打架（实测反复踩到），放文件里就没有这层转义问题。
    const evalExpr = args.evalFile
      ? fs.readFileSync(args.evalFile, "utf8").trim() : args.eval;
    if (evalExpr) {
      try {
        probeValue = await evalJS(evalExpr);
        console.log("  eval: " + (typeof probeValue === "string"
          ? probeValue : JSON.stringify(probeValue)));
      } catch (e) { console.log("  [!! ] eval 失败：" + e.message); process.exitCode = 1; }
    }

    // ---- 页面自检：基本结构 ----
    const stats = await evalJS(`(() => {
      const q = (s) => document.querySelectorAll(s).length;
      return {
        title: document.title,
        panels: q('section.panel'),
        tables: q('table'),
        rows: q('tbody tr'),
        buttons: q('button'),
        loading_left: q('.loading'),
        empty_left: q('.empty'),
        body_h: document.body.scrollHeight,
        body_w: document.body.scrollWidth,
        overflow_x: document.body.scrollWidth > window.innerWidth + 2,
        visible_text: (document.body.innerText || '').length,
      };
    })()`);
    console.log("  页面统计：" + JSON.stringify(stats));

    // ---- 可选：滚动到指定位置（配合 --viewport 分段看排版）----
    if (args.scroll) {
      await evalJS(`(() => { window.scrollTo(0, ${args.scroll}); return window.scrollY; })()`);
      await sleep(350);
    }

    // ---- 截图（整页 or 视口）----
    const shot = await p.send("Page.captureScreenshot",
      { format: "png", captureBeyondViewport: args.full, fromSurface: true });
    fs.mkdirSync(path.dirname(path.resolve(args.out)), { recursive: true });
    fs.writeFileSync(args.out, Buffer.from(shot.data, "base64"));

    const realErrors = args.keepNoise ? errors : errors.filter(
      (e) => !/favicon|ERR_/i.test(e) || !/favicon/i.test(e));
    const realFailed = failed.filter((f) => !/favicon/i.test(f));

    console.log("  截图：" + path.resolve(args.out)
      + `  (${(fs.statSync(args.out).size / 1024).toFixed(0)} KB)`);
    if (realErrors.length) {
      console.log(`  [!! ] console 报错 ${realErrors.length} 条：`);
      realErrors.slice(0, 8).forEach((e) => console.log("        · " + e));
      process.exitCode = 1;
    } else {
      console.log("  [OK ] 无 console 报错");
    }
    if (realFailed.length) {
      console.log(`  [!  ] 失败请求 ${realFailed.length} 条：` + [...new Set(realFailed)].slice(0, 5).join(" | "));
    }
    if (stats.overflow_x) {
      console.log("  [!! ] 出现横向溢出（body 宽 " + stats.body_w + " > 视口 " + args.width + "）");
      process.exitCode = 1;
    }

    // ---- 可选：把结果写成 JSON，供 ui_check.py 断言（比解析 stdout 稳）----
    if (args.json) {
      const payload = {
        url: args.url, stats: stats, probe: probeValue,
        console_errors: realErrors, failed_requests: [...new Set(realFailed)],
        screenshot: path.resolve(args.out),
        ok: process.exitCode !== 1,
      };
      fs.mkdirSync(path.dirname(path.resolve(args.json)), { recursive: true });
      fs.writeFileSync(args.json, JSON.stringify(payload, null, 2));
      console.log("  结果 JSON：" + path.resolve(args.json));
    }
  } finally {
    cleanup();
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (_) {}
  }
}

main().catch((e) => { console.error("[FAIL] " + (e && e.stack || e)); process.exit(1); });
