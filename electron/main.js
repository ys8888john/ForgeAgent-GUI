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
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

// 远程调试端口：设 FORGEAGENT_CDP_PORT=9333 就能用 CDP 驱动这个窗口
// （Runtime.evaluate 查计算样式、触事件、截图）。排查"按钮点不到/位置乱飞"
// 这类只能靠真实渲染确认的问题时非常有用 —— 静态读代码看不出来。
// 必须在 app ready 之前 appendSwitch，晚了不起作用；不设就不开，不影响正常运行。
if (process.env.FORGEAGENT_CDP_PORT) {
  app.commandLine.appendSwitch(
    "remote-debugging-port",
    String(parseInt(process.env.FORGEAGENT_CDP_PORT, 10) || 9333)
  );
}

// electron/ 的父目录就是项目根（forgeagent 包在这里，cwd 设到这才能让 `python -m forgeagent.gui` 导入到包）
const PROJECT_ROOT = path.resolve(__dirname, "..");

// python 解释器：优先用外层（forgeagent-gui）传进来的，没传就退而求其次找系统 python3。
// FORGEAGENT_PYTHON 由 forgeagent/gui/__main__.py 的 electron 分支设成 sys.executable（那个 venv 里有 agentd）。
const PYTHON = process.env.FORGEAGENT_PYTHON || "python3";
// agentd 的工作目录（用户项目），由 --cwd 传进来。__main__.py 仅在用户显式给了
// --cwd 时才设 FORGEAGENT_CWD；没给就留空 —— 此时后端走「空间（Space）」系统：
// 默认空间 ~/.agentd/spaces/default，而不是把 Electron 进程自己的 cwd 当工作区。
// （之前这里用 process.cwd() 兜底，会强行把启动目录塞成 cwd，让空间切换形同虚设。）
const FORGE_CWD = process.env.FORGEAGENT_CWD || "";

let pyProc = null;
let win = null;

// ---- 窗口状态持久化（尺寸/位置记忆）----
// 存 userData/window-state.json。商用桌面应用的基本礼貌：用户把窗口拉到
// 第二个屏幕、调大字号，重启后不该回到初始态。恢复时做越界保护：
// 分辨率变了 / 第二块屏拔了，就把位置丢掉只留尺寸（落在默认位置）。
const STATE_FILE = () => path.join(app.getPath("userData"), "window-state.json");

function loadWindowState() {
  const fallback = { width: 1120, height: 780 };
  try {
    const st = JSON.parse(fs.readFileSync(STATE_FILE(), "utf-8"));
    if (typeof st.width !== "number" || typeof st.height !== "number") return fallback;
    const { screen } = require("electron");
    const wa = screen.getPrimaryDisplay().workArea;
    const onScreen =
      typeof st.x === "number" &&
      typeof st.y === "number" &&
      st.x >= wa.x - 40 &&
      st.y >= wa.y - 40 &&
      st.x < wa.x + wa.width &&
      st.y < wa.y + wa.height;
    return onScreen ? st : { width: st.width, height: st.height };
  } catch (_) {
    return fallback; // 没存过 / 坏文件：初始尺寸
  }
}

function saveWindowState() {
  if (!win) return;
  try {
    const b = win.getBounds();
    fs.mkdirSync(path.dirname(STATE_FILE()), { recursive: true });
    fs.writeFileSync(STATE_FILE(), JSON.stringify(b), "utf-8");
  } catch (_) {
    /* 磁盘抽风不拦退出流程 */
  }
}

// 拉起 `python -m forgeagent.gui --mode serve`，逐行读 stdout 抓 UI_READY <url>。
function startBackend() {
  return new Promise((resolve, reject) => {
    // 只在用户显式 --cwd 时才把工作目录传给后端；否则让空间系统接管（见上）。
    const args = ["-m", "forgeagent.gui", "--mode", "serve"];
    if (FORGE_CWD) args.push("--cwd", FORGE_CWD);
    pyProc = spawn(
      PYTHON,
      args,
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
  const state = loadWindowState();
  win = new BrowserWindow({
    width: state.width,
    height: state.height,
    x: state.x,
    y: state.y,
    minWidth: 720,
    minHeight: 480,
    backgroundColor: "#ffffff",
    // 界面只用 fetch 跟本机后端通信，不需要 node 能力；按最小权限关掉 nodeIntegration。
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  if (process.platform !== "darwin") win.removeMenu();
  win.loadURL(url);
  let saveTimer = null;
  const scheduleSave = () => {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveWindowState, 500); // 防抖：拖动中别疯狂写盘
  };
  win.on("moved", scheduleSave);
  win.on("resized", scheduleSave);
  win.on("close", saveWindowState);
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
  if (!pyProc) return;
  const pid = pyProc.pid;
  pyProc = null;
  if (!pid) return;
  if (process.platform === "win32") {
    // Windows 上 kill("SIGTERM") 走 TerminateProcess，只杀 pyProc 自己，
    // 它的子孙（agentd worker、以及 server.py 里那个保活 WSL 的
    // `wsl -e sleep infinity`）会活下来 —— 后者尤其麻烦：它一旦活着，
    // WSL 发行区就永远不会被空闲回收，内存和显存一直占着。/T 连树一起杀。
    try {
      spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore" });
    } catch (_) {
      try {
        process.kill(pid);
      } catch (_) {
        /* 已退出 */
      }
    }
  } else {
    // POSIX：subprocess 默认可被信号打断，kill 进程组即可带走子孙
    try {
      process.kill(pid, "SIGTERM");
    } catch (_) {
      /* 已退出 */
    }
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
