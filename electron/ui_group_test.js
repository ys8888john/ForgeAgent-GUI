// 工具调用分组（tool-group）UI 行为验证 harness。
// 用法：electron.exe out/ui_group_test.js
// 加载真实 index.html，直接调用页面全局的 upsertTool()，复刻
// 2026-10-11 用户截图场景（一轮里 list_dir + web_search + read×N + grep
// + make_xlsx，且工具卡之间穿插 Thinking/正文气泡），断言「按轮归组」：
//   · 单调用保持裸卡、不包壳
//   · 同轮第二张起包成「工具调用 ×N」组
//   · 组中穿插正文/思考气泡不切组（修复旧「相邻归组」的核心 bug）
//   · 运行中展开、全部终态后自动收成一枚标签
//   · 新一轮（startNewRound）切断、不与旧轮缝一起
//   · 失败聚合 / 点击展开收起 / 迟到 update 反推组头
//   · 历史重放走相邻归组（不同历史轮靠气泡自然切开）
const { app, BrowserWindow } = require("electron");
const path = require("path");
const fs = require("fs");
const LOG = path.join(__dirname, "..", "out", "ui_group_test.log");
function report(line) { fs.appendFileSync(LOG, line + "\n"); }
try { fs.unlinkSync(LOG); } catch (e) {}
process.on("uncaughtException", function (e) { report("UNCAUGHT: " + (e && e.stack || e)); app.exit(2); });

app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("no-sandbox");

app.whenReady().then(() => {
  report("app ready");
  const win = new BrowserWindow({ show: false, width: 1100, height: 900 });
  const page = path.join(__dirname, "..", "forgeagent", "gui", "assets", "index.html");
  win.webContents.on("did-fail-load", function (_e, code, desc, url) {
    report("DID-FAIL-LOAD " + code + " " + desc + " " + url);
  });
  win.webContents.on("console-message", function (_e, level, message, line, sourceId) {
    if (level >= 2) report("RENDERER[" + level + "] " + message + " @" + sourceId + ":" + line);
  });
  win.webContents.on("did-finish-load", async () => {
    report("did-finish-load");
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
      report("HARNESS ERROR: " + (e && e.message));
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
  results.push({ name: "0. 测试句柄存在", ok: true });

  const msgs = T.messages;
  const q = (sel) => msgs.querySelectorAll(sel);
  const freshRound = () => { msgs.innerHTML = ""; T.startNewRound(); };

  // ===== 场景 A：一轮 14 连调用 + 中间穿插 Thinking/正文气泡（复刻用户截图）=====
  freshRound();
  // 首张裸卡
  T.upsertTool({ id: "c1", title: "list_dir 工作区", kind: "execute", status: "in_progress" });
  ok("A1. 单卡不包组", q(".bubble.tool").length === 1 && q(".tool-group").length === 0);

  // 第二张 → 包成组 ×2；当前 c2 在跑，按轮逻辑运行中应展开（而非折叠）
  T.upsertTool({ id: "c2", title: "web_search 成都-北京航班", kind: "search", status: "in_progress" });
  var g = q(".tool-group")[0];
  ok("A2a. 第二张卡包成组", !!g && g.classList.contains("tool-group"));
  ok("A2b. 组内恰好 2 张卡", !!g && g._body.children.length === 2);
  ok("A2c. 运行中展开（便于看实时进度）", !!g && !g.classList.contains("collapsed"));
  ok("A2d. 头部=运行中·web_search",
    !!g && g.classList.contains("running") &&
    /运行中/.test(g._st.textContent) && /web_search/.test(g._st.textContent));
  ok("A2e. 计数 ×2", !!g && g._count.textContent === "×2");

  // 模拟内核在一轮里先想了想（Thinking 气泡），再继续调工具 —— 旧「相邻归组」
  // 会因此把后面的卡切成新框；按轮归组必须无视这层气泡，全部并回同一组。
  var think = document.createElement("div");
  think.className = "bubble thought";
  think.textContent = "我需要先看看目录结构，再逐条读取航班数据…";
  msgs.appendChild(think);

  T.upsertTool({ id: "c1", title: "list_dir 工作区", kind: "execute", status: "completed", output: "目录树" });
  T.upsertTool({ id: "c2", title: "web_search 成都-北京航班", kind: "search", status: "completed", output: "命中 12 条" });
  for (var i = 3; i <= 14; i++) {
    T.upsertTool({ id: "c" + i, title: "read_file 航班数据" + i, kind: "read", status: "completed", output: "行程" + i });
  }
  // 再插一段正文气泡，随后又调一个工具 —— 仍应并回同一组
  var body = document.createElement("div");
  body.className = "bubble";
  body.textContent = "已读取全部航班数据，再做一次筛选。";
  msgs.appendChild(body);
  T.upsertTool({ id: "c15", title: "grep 筛选结果", kind: "search", status: "completed", output: "命中 5 行" });

  ok("A3a. 穿插气泡后所有卡仍在同一组", q(".tool-group").length === 1);
  ok("A3b. 14 连 + 末张 = 15 张全在一组", !!g && g._body.children.length === 15);
  ok("A3c. 计数 ×15", !!g && g._count.textContent === "×15");
  ok("A3d. 全完成→组头 done 态并自动收成标签",
    !!g && g.classList.contains("done") && /完成/.test(g._st.textContent) && g.classList.contains("collapsed"));
  ok("A3e. read/grep 卡自身的默认折叠不受影响",
    g._body.children[14].classList.contains("collapsed"));

  // ===== 场景 B：极简穿插 —— 卡/气泡/卡/气泡/卡 全进一组 ×3 =====
  freshRound();
  T.upsertTool({ id: "b1", title: "read 配置", kind: "read", status: "completed", output: "cfg" });
  var tb1 = document.createElement("div"); tb1.className = "bubble thought"; tb1.textContent = "看看配置"; msgs.appendChild(tb1);
  T.upsertTool({ id: "b2", title: "exec 检查", kind: "execute", status: "completed", output: "ok" });
  var tb2 = document.createElement("div"); tb2.className = "bubble"; tb2.textContent = "正文"; msgs.appendChild(tb2);
  T.upsertTool({ id: "b3", title: "grep 查", kind: "search", status: "completed", output: "x" });
  var gb = q(".tool-group")[0];
  ok("B1. 卡/气泡交替仍归一组", q(".tool-group").length === 1);
  ok("B2. 组内 3 张", !!gb && gb._body.children.length === 3);
  ok("B3. 计数 ×3", !!gb && gb._count.textContent === "×3");

  // ===== 场景 C：新一轮切断，不与旧轮缝一起 =====
  // 注意：freshRound 已在场景 B 清空了消息流，这里 msgs 里只剩 B 轮的 3 张组 gb。
  T.startNewRound();                 // 模拟「用户又提了一个问题」
  T.upsertTool({ id: "n1", title: "list_dir 新一轮", kind: "execute", status: "in_progress" });
  // 裸卡 = 不在任何 .tool-group 内的 .bubble.tool
  var bareNow = Array.from(q(".bubble.tool")).filter(function (el) { return !el.closest(".tool-group"); });
  ok("C1. 新一轮首张是裸卡", bareNow.length === 1);
  T.upsertTool({ id: "n2", title: "grep 新一轮", kind: "search", status: "in_progress" });
  var groups = q(".tool-group");
  var gNew = groups[groups.length - 1];   // 新轮组是最后一个
  ok("C2. 新轮第二张包成独立新组", groups.length === 2 && !!gNew && gNew._body.children.length === 2);
  // 旧轮组（gb，3 张）保持独立、未被缝进新轮；新轮组仅 2 张
  ok("C3. 旧轮组未被缝进新轮（旧组3张独立、新组2张）",
    groups[0]._body.children.length === 3 && gNew._body.children.length === 2);

  // ===== 场景 D：失败聚合 / 头点击 / 迟到 update =====
  T.upsertTool({ id: "n2", title: "grep 新一轮", kind: "search", status: "completed", output: "命中 3 行" });
  T.upsertTool({ id: "n1", title: "list_dir 新一轮", kind: "execute", status: "completed", output: "ok" });
  T.upsertTool({ id: "n3", title: "make_xlsx 生成表", kind: "execute", status: "failed", output: "磁盘满" });
  ok("D1. 失败态聚合到组头（×3）",
    gNew.classList.contains("failed") && /失败/.test(gNew._st.textContent) && gNew._count.textContent === "×3");

  var head = gNew.querySelector(".tool-group-head");
  head.click();
  ok("D2a. 点击展开", !gNew.classList.contains("collapsed"));
  head.click();
  ok("D2b. 再点收起", gNew.classList.contains("collapsed"));

  T.upsertTool({ id: "n1", title: "list_dir 新一轮", kind: "execute", status: "in_progress" });
  ok("D3. 重跑→组头回运行中·list_dir",
    gNew.classList.contains("running") && /list_dir/.test(gNew._st.textContent));

  // ===== 场景 E：历史重放走相邻归组（不同历史轮靠气泡自然切开）=====
  T.setReplaying(true);
  freshRound();
  T.upsertTool({ id: "h1", title: "旧轮 read", kind: "read", status: "completed", output: "a" });
  T.upsertTool({ id: "h2", title: "旧轮 grep", kind: "search", status: "completed", output: "b" });
  var hb = document.createElement("div"); hb.className = "bubble"; hb.textContent = "（旧轮正文）"; msgs.appendChild(hb);
  T.upsertTool({ id: "h3", title: "另一轮 read", kind: "read", status: "completed", output: "c" });
  T.upsertTool({ id: "h4", title: "另一轮 grep", kind: "search", status: "completed", output: "d" });
  T.setReplaying(false);
  var hGroups = q(".tool-group");
  ok("E1. 历史重放：相邻两段各成一组（气泡切开两段，不按实时轮糊成一组）",
    hGroups.length === 2 && hGroups[0]._body.children.length === 2 && hGroups[1]._body.children.length === 2);

  return results;
}
