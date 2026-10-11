// 视觉证据：构造「14 连工具调用」场景，截折叠态 / 展开态两张图。
const { app, BrowserWindow } = require("electron");
const path = require("path"), fs = require("fs");

app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("no-sandbox");

app.whenReady().then(() => {
  const win = new BrowserWindow({ show: false, width: 780, height: 940 });
  win.loadFile(path.join(__dirname, "..", "forgeagent", "gui", "assets", "index.html"));
  win.webContents.on("did-finish-load", async () => {
    try {
      const scene = "(function(){\n" +
        "var T = window.__faTest; var msgs = T.messages; msgs.innerHTML = ''; T.resetToolCards();\n" +
        "function bub(cls, txt){ var d=document.createElement('div'); d.className='bubble '+cls; d.textContent=txt; msgs.appendChild(d); return d; }\n" +
        "bub('user','搜成都到北京的航班 整理成 Excel');\n" +
        "T.upsertTool({id:'c1',title:'list_dir 工作区',kind:'execute',status:'completed',output:'目录树'});\n" +
        "T.upsertTool({id:'c2',title:'web_search 成都-北京航班',kind:'search',status:'completed',output:'共 12 条结果'});\n" +
        "T.upsertTool({id:'c3',title:'web_fetch 抓取详情页',kind:'fetch',status:'completed',output:'……'});\n" +
        "for(var i=4;i<14;i++) T.upsertTool({id:'c'+i,title:'read_file 航班数据'+i,kind:'read',status:'completed',output:'行程'+i});\n" +
        "T.upsertTool({id:'c14',title:'make_xlsx 生成表格',kind:'execute',status:'completed',output:'已新建 Excel 文件 out/航班列表.xlsx'});\n" +
        "bub('', '已整理成 Excel，共 12 条航班信息……');\n" +
        "return 'scene ok';\n" +
        "})()";
      await win.webContents.executeJavaScript(scene);
      await new Promise(r => setTimeout(r, 300));
      const img1 = await win.webContents.capturePage();
      fs.writeFileSync(path.join(__dirname, "..", "out", "group_collapsed.png"), img1.toPNG());
      await win.webContents.executeJavaScript(
        "(function(){ document.querySelector('.tool-group .tool-group-head').click(); return 1; })()");
      await new Promise(r => setTimeout(r, 300));
      const img2 = await win.webContents.capturePage();
      fs.writeFileSync(path.join(__dirname, "..", "out", "group_expanded.png"), img2.toPNG());
      app.exit(0);
    } catch (e) {
      fs.writeFileSync(path.join(__dirname, "..", "out", "shot_err.log"), String(e && e.stack || e));
      app.exit(2);
    }
  });
});
