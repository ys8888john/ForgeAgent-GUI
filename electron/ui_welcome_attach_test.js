// 欢迎页图片附件 smoke test。
// 用法：electron.exe electron/ui_welcome_attach_test.js
// 背景：图片附件（📎/粘贴/拖拽/缩略图条）最初只加在聊天页 #composer 上，
// 而新会话第一眼看到的是欢迎页 #welcome（独立 textarea #w-input），
// 用户反馈「还是没法发送和粘贴图片」就是发在欢迎页。本文件锁住欢迎页侧：
//   W1. #w-attach 按钮与 #w-attach-strip 存在
//   W2. 注入待发图后两个缩略图条同步渲染（.att-thumb ×1）
//   W3. 点欢迎页缩略图的 × → 待发队列与两个条都清空
//   W4. 纯图片无文本，欢迎页 Enter 也能发出（/api/send body 带 images id）
//   W5. 文本+图片混合发送，body 同时带 text 与 images
const { app, BrowserWindow } = require("electron");
const path = require("path");
const fs = require("fs");
const LOG = path.join(__dirname, "..", "out", "ui_welcome_attach_test.log");
function report(line) { fs.appendFileSync(LOG, line + "\n"); }
try { fs.unlinkSync(LOG); } catch (e) {}
process.on("uncaughtException", function (e) { report("UNCAUGHT: " + (e && e.stack || e)); app.exit(2); });

app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("no-sandbox");

app.whenReady().then(() => {
  const win = new BrowserWindow({ show: false, width: 1100, height: 900 });
  const page = path.join(__dirname, "..", "forgeagent", "gui", "assets", "index.html");
  win.webContents.on("console-message", function (_e, level, message, line, sourceId) {
    if (level >= 2) report("RENDERER[" + level + "] " + message + " @" + sourceId + ":" + line);
  });
  win.webContents.on("did-finish-load", async () => {
    try {
      const results = await win.webContents.executeJavaScript("(" + testMain.toString() + ")()");
      let pass = 0, fail = 0;
      for (const r of results) {
        if (r.ok) { pass++; report("PASS  " + r.name); }
        else { fail++; report("FAIL  " + r.name + (r.extra ? "   [" + r.extra + "]" : "")); }
      }
      report("SUMMARY: " + pass + " pass, " + fail + " fail");
      app.exit(fail === 0 ? 0 : 1);
    } catch (e) {
      report("HARNESS ERROR: " + (e && e.stack || e));
      app.exit(2);
    }
  });
  win.loadFile(page);
  setTimeout(function () { report("TIMEOUT: no did-finish-load in 20s"); app.exit(3); }, 20000);
});

function testMain() {
  const results = [];
  const ok = (name, cond, extra) =>
    results.push({ name: name, ok: !!cond, extra: extra == null ? "" : String(extra) });
  const T = window.__faTest;
  if (!T) { results.push({ name: "0. 测试句柄存在", ok: false, extra: "window.__faTest undefined" }); return results; }

  const wAttach = document.getElementById("w-attach");
  const wStrip = document.getElementById("w-attach-strip");

  // W1. 欢迎页附件元素存在
  ok("W1. #w-attach 与 #w-attach-strip 存在", !!wAttach && !!wStrip,
    "w-attach=" + !!wAttach + " strip=" + !!wStrip);

  // W2. 注入假待发图 → 两个条同步渲染
  T.addPendingImageMeta({ id: "fake-id-1", url: "data:image/png;base64,iVBORw0KGgo=", name: "t.png" });
  const mainStrip = document.getElementById("attach-strip");
  ok("W2a. 主条渲染 1 张缩略图", mainStrip.querySelectorAll(".att-thumb").length === 1,
    "main=" + mainStrip.querySelectorAll(".att-thumb").length);
  ok("W2b. 欢迎页条渲染 1 张缩略图", wStrip.querySelectorAll(".att-thumb").length === 1,
    "welcome=" + wStrip.querySelectorAll(".att-thumb").length);

  // W3. 点欢迎页缩略图的 × → 队列与两个条都清空
  const x = wStrip.querySelector(".att-thumb .att-x");
  if (x) x.click();
  ok("W3. 点 × 后队列与两条都清空",
    T.pendingCount() === 0 &&
    wStrip.querySelectorAll(".att-thumb").length === 0 &&
    mainStrip.querySelectorAll(".att-thumb").length === 0,
    "pending=" + T.pendingCount());

  // stub fetch：捕获 /api/send 的 body，返回 ok
  const sent = [];
  window.fetch = function (path, opts) {
    sent.push({ path: path, body: opts && opts.body ? JSON.parse(opts.body) : null });
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ ok: true }); } });
  };
  T.setStarted(true);

  // W4. 纯图片无文本，欢迎页 Enter 发送
  T.addPendingImageMeta({ id: "img-only", url: "data:image/png;base64,iVBORw0KGgo=", name: "b.png" });
  T.wInputEl.value = "";
  T.wInputEl.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  const w4 = sent.find(function (s) { return s.path === "/api/send"; });
  ok("W4. 纯图 Enter 触发 /api/send 且 images=[img-only]",
    !!w4 && w4.body.images && w4.body.images.length === 1 && w4.body.images[0] === "img-only" && !w4.body.text,
    JSON.stringify(w4 ? w4.body : null));

  // W5. 文本+图片混合
  sent.length = 0;
  T.addPendingImageMeta({ id: "img-mix", url: "data:image/png;base64,iVBORw0KGgo=", name: "c.png" });
  T.wInputEl.value = "看这张图";
  T.wInputEl.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  const w5 = sent.find(function (s) { return s.path === "/api/send"; });
  ok("W5. 图文混发 body 带 text 与 images",
    !!w5 && w5.body.text === "看这张图" && w5.body.images && w5.body.images[0] === "img-mix",
    JSON.stringify(w5 ? w5.body : null));

  // 清场：欢迎页输入框还原
  T.wInputEl.value = "";
  return results;
}
