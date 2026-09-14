"use strict";

// ForgeAgent 的 Electron 壳 —— 参考 WorkBuddy 的前端形态：
// 一个 Electron 应用，UI 是它自带 Chromium 渲染的 web 页面（forgeagent/gui/assets/index.html）。
//
// 为什么不用 pywebview（windows 上的 Edge WebView2）：
//   这台机器 GPU/driver 有问题，WebView2 的浏览器进程会崩，窗口能开但 JS 跑两下就死。
//   Electron 自带一套 Chromium + 软件渲染兜底（swiftshader），对同样的 GPU 问题钝感得多，
//   跟 WorkBuddy 自己能在崩机王上跑稳是同一套道理。
//
// 职责划分（和 serve 模式一致）：
//   本进程只负责「拉起 Python 后端 + 开一个 Chromium 窗口把 URL 交过去」，
//   不碰任何业务逻辑。后端还是 UiServer + agentd（ACP 只在 stdio）。
//   token 由 UiServer 直接内联进 index.html（serve.py 的 _file 已做），页面 fetch 时带 header，
//   所以这里不需要任何 node <-> 页面的桥。

const { app, BrowserWindow } = require("electron");
const { spawn } = require("child_process");
const path = require("path");

// electron/ 的父目录就是项目根（forgeagent 包在这里，cwd 设到这才能让 `python -m forgeagent.gui` 导入到包）
const PROJECT_ROOT = path.resolve(__dirname, "..");

// python 解释器：优先用外层（forgeagent-gui）传进来的，没传就退而求其次找系统 python3。
// FORGEAGENT_PYTHON 由 forgeagent/gui/__main__.py 的 electron 分支设成 sys.executable（那个 venv 里有 agentd）。
const PYTHON = process.env.FORGEAGENT_PYTHON || "python3";
// agentd 的工作目录（用户项目），由 --cwd 传进来。
const AGENTD_CWD = process.env.FORGEAGENT_CWD || process.cwd();

let pyProc = null;
let win = null;

// 拉起 `python -m forgeagent.gui --mode serve`，逐行读 stdout 抓 UI_READY <url>。
function startBackend() {
  return new Promise((resolve, reject) => {
    pyProc = spawn(
      PYTHON,
      ["-m", "forgeagent.gui", "--mode", "serve", "--cwd", AGENTD_CWD],
      { cwd: PROJECT_ROOT, env: process.env, stdio: ["ignore", "pipe", "pipe"] }
    );

    let rest = "";
    const onData = (chunk) => {
      rest += chunk.toString();
      const lines = rest.split("\n");
      rest = lines.pop(); // 最后一段可能不完整，留到下次
      for (const raw of lines) {
        const line = raw.trim();
        if (line.startsWith("UI_READY ")) {
          resolve(line.split(/\s+/)[1].trim());
          return;
        }
        if (line) console.log("[forgeagent:py]", line);
      }
    };

    pyProc.stdout.on("data", onData);
    pyProc.stderr.on("data", (d) => {
      d.toString().split("\n").forEach((l) => l.trim() && console.error("[forgeagent:py!]", l));
    });
    pyProc.on("error", (e) => reject(e));
    pyProc.on("exit", (code) => {
      // 后端自己退了（没先打印 UI_READY）：多半是 python/依赖问题，把错误抛给上层
      if (!win) reject(new Error("agent 后端进程退出，退出码 " + code));
    });

    // 兜底超时：15 秒还没 UI_READY 就报错，避免窗口永远白屏
    setTimeout(() => reject(new Error("15 秒内没等到 UI_READY，agent 后端可能没起来")), 15000);
  });
}

function createWindow(url) {
  win = new BrowserWindow({
    width: 1120,
    height: 780,
    minWidth: 720,
    minHeight: 480,
    backgroundColor: "#ffffff",
    // 界面只用 fetch 跟本机后端通信，不需要 node 能力；按最小权限关掉 nodeIntegration。
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  if (process.platform !== "darwin") win.removeMenu();
  win.loadURL(url);
  win.on("closed", () => {
    win = null;
  });
}

function showError(msg) {
  win = new BrowserWindow({ width: 640, height: 360, title: "ForgeAgent 启动失败" });
  if (process.platform !== "darwin") win.removeMenu();
  win.loadURL(
    "data:text/html," +
      encodeURIComponent(
        "<meta charset='utf-8'><body style='font-family:sans-serif;padding:24px'>" +
          "<h2>ForgeAgent 启动失败</h2><pre style='white-space:pre-wrap'>" +
          msg +
          "</pre><p>确认：python 能跑 `python -m forgeagent.gui --mode serve`，且已装 agentd。</p></body>"
      )
  );
}

function cleanup() {
  if (pyProc) {
    try {
      pyProc.kill("SIGTERM");
    } catch (_) {
      /* 已退出 */
    }
    pyProc = null;
  }
}

app.whenReady().then(async () => {
  try {
    const url = await startBackend();
    createWindow(url);
  } catch (e) {
    showError(String(e && e.message ? e.message : e));
  }

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0 && win === null) {
      startBackend()
        .then(createWindow)
        .catch((e) => showError(String(e && e.message ? e.message : e)));
    }
  });
});

// 窗口全关 -> 收掉 Python 后端 -> 退出（macOS 习惯上保留 app，但这里也一并退出更省心）
app.on("window-all-closed", () => {
  cleanup();
  if (process.platform !== "darwin") app.quit();
});
app.on("before-quit", cleanup);
app.on("quit", cleanup);
