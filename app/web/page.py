"""网页监控前端页面（单文件内嵌，避免打包时漏掉静态资源）。

画面通过「单连接多路复用」传输：一条 `/api/live` 连接里同时推送所有画面的 JPEG
与状态 JSON，避开浏览器「同域最多 6 条长连接」的限制（原来每路 MJPEG 各占一条，
第 7 路就会一直排队）。若浏览器不支持流式读取，会自动退回逐路 MJPEG + 轮询状态。
"""

INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>拓竹打印机监控台</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#151c21">
<link rel="icon" type="image/png" href="/icon-192.png">
<link rel="apple-touch-icon" href="/icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="打印机监控">
<style>
  :root{
    --bg:#0d1114; --panel:#151c21; --panel2:#1b242a; --border:#26333a;
    --text:#e6edf1; --dim:#8fa3ad; --accent:#25c2d6; --ok:#2ecc71;
    --warn:#f5a623; --err:#ff5d5d;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:var(--bg);color:var(--text);
    font-family:"Microsoft YaHei UI","Microsoft YaHei",system-ui,-apple-system,sans-serif}
  header{display:flex;align-items:center;gap:12px;padding:10px 14px;background:var(--panel);
    border-bottom:1px solid var(--border);position:sticky;top:0;z-index:10;flex-wrap:wrap}
  header h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.5px}
  #summary{font-size:12px;color:var(--dim)}
  header .grow{flex:1}
  header button,header select{background:var(--panel2);color:var(--text);border:1px solid var(--border);
    border-radius:5px;padding:5px 10px;font-size:12px;cursor:pointer}
  header button:hover{border-color:var(--accent)}
  #mode{font-size:11px;color:var(--dim);border:1px solid var(--border);border-radius:10px;padding:2px 8px}
  #clock{font-family:Consolas,monospace;font-size:12px;color:var(--dim);min-width:150px;text-align:right}
  #wall{display:grid;gap:8px;padding:8px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
  #wall.cols1{grid-template-columns:1fr}
  #wall.cols2{grid-template-columns:repeat(2,1fr)}
  #wall.cols3{grid-template-columns:repeat(3,1fr)}
  #wall.cols4{grid-template-columns:repeat(4,1fr)}
  .tile{background:var(--panel);border:1px solid var(--border);border-radius:6px;overflow:hidden;
    display:flex;flex-direction:column;min-height:180px}
  .tile.full{position:fixed;inset:0;z-index:50;border-radius:0;border:none}
  .video{position:relative;background:#05090b;flex:1;min-height:120px;overflow:hidden}
  .video img{width:100%;height:100%;object-fit:contain;display:block}
  .video .nosignal{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    color:var(--dim);font-size:12px}
  /* 「本机根本出不了画面」的说明：直接盖在画面上，触屏用户也能看到 */
  .video .novideo{position:absolute;inset:auto 0 0 0;background:rgba(0,0,0,.78);color:var(--warn);
    font-size:11px;line-height:1.5;padding:6px 8px;white-space:pre-line;text-align:left}
  .overlay{position:absolute;left:8px;top:6px;background:rgba(0,0,0,.55);padding:2px 8px;border-radius:3px;
    font-size:12px;max-width:70%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .status{position:absolute;right:8px;top:6px;background:rgba(0,0,0,.55);padding:2px 8px;border-radius:3px;
    font-size:11px;display:flex;align-items:center;gap:5px}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--dim)}
  .bar{background:var(--panel2);padding:6px 9px 7px;display:flex;flex-direction:column;gap:4px}
  .bar .row1{display:flex;align-items:center;gap:8px;font-size:12px}
  .badge{border:1px solid var(--dim);border-radius:3px;padding:0 6px;font-size:11px;font-weight:600;color:var(--dim)}
  .pct{font-weight:700;color:var(--accent);font-size:13px}
  .task{color:var(--dim);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .remain{color:var(--dim);white-space:nowrap}
  .progress{height:8px;background:#101a1f;border:1px solid var(--border);border-radius:4px;overflow:hidden}
  .progress i{display:block;height:100%;width:0;background:var(--accent);transition:width .3s}
  .row2{display:flex;gap:14px;font-size:12px;color:var(--dim);flex-wrap:wrap}
  .row2 b{color:var(--accent);font-weight:700}
  .err{color:var(--err);font-weight:700}
  .filament{display:flex;gap:10px;flex-wrap:wrap;font-size:11px;color:var(--dim)}
  .chip{display:flex;align-items:center;gap:4px}
  .sw{width:10px;height:10px;border-radius:2px;border:1px solid #000}
  .chip.active{color:var(--accent);font-weight:700}
  /* 设备详情（第三方族能给出十几条读数：风扇、断料/走料传感器、耗材用量、主机负载…）。
     为什么默认收起：这些读数全铺开时卡片会比画面还高，一屏就被读数占满 ——
     画面才是主角，所以做成按需展开的一块。
     窄屏（手机一行两列、长读数名）也看得清：table-layout:fixed + 折行，不用横向滚动。 */
  .details{margin-top:2px;font-size:11px;color:var(--dim)}
  .details > summary{cursor:pointer;color:var(--accent);font-size:11px;outline:none}
  .details table{width:100%;border-collapse:collapse;margin-top:3px;table-layout:fixed}
  .details td{padding:1px 3px;vertical-align:top;word-break:break-word;overflow-wrap:anywhere}
  .details td.k{width:40%;color:var(--dim)}
  .details td.v{color:var(--text)}
  .row4{display:flex;align-items:center;gap:10px;font-size:11px;color:var(--dim);flex-wrap:wrap}
  .row4 .grow{flex:1}
  .row4 button{background:var(--panel);color:var(--text);border:1px solid var(--border);
    border-radius:4px;padding:2px 8px;font-size:11px;cursor:pointer}
  .row4 button:hover{border-color:var(--accent)}
  .row4 button:disabled{color:#5a6a72;cursor:default}
  .hms{color:var(--err);border:1px solid var(--err);border-radius:3px;padding:0 5px;
    font-weight:700;cursor:pointer}
  #hmsbox{position:fixed;inset:0;background:rgba(6,10,12,.9);display:none;z-index:80;
    padding:24px;overflow:auto}
  #hmsbox .card{background:var(--panel);border:1px solid var(--border);border-radius:8px;
    padding:14px 16px;max-width:760px;margin:0 auto 12px}
  #hmsbox a{color:var(--accent)}
  #toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:var(--panel2);
    border:1px solid var(--accent);border-radius:6px;padding:8px 16px;font-size:13px;display:none;z-index:90}
  #login{position:fixed;inset:0;background:rgba(6,10,12,.92);display:flex;align-items:center;
    justify-content:center;flex-direction:column;gap:12px;z-index:99}
  #login input{padding:8px 10px;border-radius:5px;border:1px solid var(--border);background:var(--panel2);
    color:var(--text);font-size:14px;width:240px}
  #login button{padding:8px 16px;border-radius:5px;border:1px solid var(--accent);background:var(--accent);
    color:#04222a;font-weight:700;cursor:pointer}
  @media (max-width:640px){
    #wall{grid-template-columns:1fr;gap:6px;padding:6px}
    header h1{font-size:13px}
    #clock{display:none}
    .tile[style*="span 2"],.tile[style*="span 3"]{grid-column:span 1 !important;grid-row:span 1 !important}
  }

  /* ---------------------------------------------------------------- 设备管理与设置
     桌面端有工具栏按钮与对话框，网页端原先什么都没有 ——
     安卓版没有桌面界面，网页是唯一入口，因此这些必须补齐。 */
  #modal{position:fixed;inset:0;background:rgba(6,10,12,.88);display:none;z-index:90;
         align-items:center;justify-content:center;padding:16px}
  #modal .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;
               padding:18px;width:100%;max-width:560px;max-height:88vh;overflow:auto}
  #modal h2{margin:0 0 4px;font-size:16px;color:var(--accent)}
  #modal .hint{color:var(--dim);font-size:12px;line-height:1.7;margin-bottom:12px}
  #modal label{display:block;color:var(--dim);font-size:12px;margin:10px 0 4px}
  #modal input,#modal select{width:100%;box-sizing:border-box;background:#0d1114;
        border:1px solid var(--border);border-radius:6px;color:var(--fg);
        padding:8px 10px;font-size:14px;font-family:inherit}
  /* 配置备份用的多行文本框：内容可能几 KB，必须能滚动、能选中复制 */
  #modal textarea{width:100%;box-sizing:border-box;background:#0d1114;margin:8px 0;
        border:1px solid var(--border);border-radius:6px;color:var(--fg);
        padding:8px 10px;font-size:12px;font-family:ui-monospace,Consolas,monospace;
        min-height:150px;resize:vertical;user-select:text;-webkit-user-select:text}
  #modal .conn-row input{flex:1;min-width:0}
  #modal .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  #modal .row > *{flex:1 1 140px}
  #modal button{background:#1d272e;border:1px solid var(--border);border-radius:6px;
        color:var(--fg);padding:8px 14px;font-size:13px;cursor:pointer;font-family:inherit}
  #modal button:hover{border-color:var(--accent);color:var(--accent)}
  #modal button.primary{background:var(--accent);color:#06222a;border-color:var(--accent);font-weight:bold}
  #modal button.danger:hover{border-color:var(--err);color:var(--err)}
  #modal .actions{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
/* 连接信息（设置对话框底部）：地址与令牌要能一眼看清、一键复制 */
#modal .conn-row{display:flex;align-items:center;gap:8px;margin:6px 0;
  background:#111a1f;border:1px solid var(--border);border-radius:6px;padding:6px 8px}
#modal .conn-url,#modal .conn-token{flex:1;min-width:0;overflow-x:auto;white-space:nowrap;
  font-size:12px;color:var(--accent);background:none;padding:0}
#modal .conn-token{color:#ffd479;letter-spacing:1px;user-select:all}
#modal .conn-label{color:var(--dim);font-size:12px;flex:0 0 auto}
#modal .conn-copy{flex:0 0 auto !important;padding:4px 10px !important;font-size:12px}
  #modal .list{margin-top:10px;display:flex;flex-direction:column;gap:6px}
  #modal .item{display:flex;align-items:center;gap:8px;background:#111a1f;
        border:1px solid var(--border);border-radius:6px;padding:8px 10px;font-size:13px}
  #modal .item .name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #modal .item .meta{color:var(--dim);font-size:11px}
  #modal .item .tag{font-size:11px;color:var(--dim);border:1px solid var(--border);
        border-radius:3px;padding:0 5px}
  #modal .empty{color:var(--dim);font-size:13px;padding:12px;text-align:center}
  #modal .status{color:var(--dim);font-size:12px;margin-top:10px;min-height:16px}
  #menu{position:fixed;z-index:95;background:var(--panel);border:1px solid var(--border);
        border-radius:8px;padding:4px;display:none;min-width:150px;box-shadow:0 6px 24px rgba(0,0,0,.5)}
  #menu button{display:block;width:100%;text-align:left;background:none;border:0;color:var(--fg);
        padding:8px 12px;font-size:13px;cursor:pointer;border-radius:5px;font-family:inherit}
  #menu button:hover{background:#1d272e;color:var(--accent)}
  #menu .sep{height:1px;background:var(--border);margin:4px 2px}
  /* 控制被固件挡住时的提示条（触屏看不到 tooltip，必须有可见提示） */
  .ctrl-hint{color:var(--warn,#e0a030);border:1px solid var(--warn,#e0a030);
        border-radius:3px;padding:0 6px;cursor:pointer;font-size:11px;white-space:nowrap}
  .ctrl-hint:hover{background:rgba(224,160,48,.12)}
</style>
</head>
<body>
<header>
  <h1>拓竹打印机监控台</h1>
  <div id="summary">连接中…</div>
  <span id="mode">—</span>
  <div class="grow"></div>
  <select id="cols" title="每行显示多少路">
    <option value="0">自动布局</option>
    <option value="1">1 列</option>
    <option value="2">2 列</option>
    <option value="3">3 列</option>
    <option value="4">4 列</option>
  </select>
  <button id="btn-install" style="display:none">安装到桌面</button>
  <button id="btn-discover" title="扫描局域网里的打印机">🔍 自动搜索</button>
  <button id="btn-add" title="手动添加打印机">＋ 添加</button>
  <button id="btn-manage" title="管理已添加的打印机">⚙ 设备</button>
  <button id="btn-settings" title="帧率 / 刷新 / 网页参数">⚙ 设置</button>
  <button id="btn-reload">刷新</button>
  <div id="clock"></div>
</header>
<main id="wall"></main>

<div id="hmsbox"></div>
<div id="toast"></div>
<div id="modal"><div class="card" id="modal-card"></div></div>
<div id="menu"></div>

<div id="login" style="display:none">
  <div style="color:#8fa3ad;font-size:13px">请输入软件里显示的访问令牌</div>
  <input id="token-input" placeholder="访问令牌" autocomplete="off">
  <button id="token-ok">进入</button>
</div>

<script>
"use strict";
const state = {tiles: new Map(), token: "", live: false, liveFails: 0, fallback: false,
               // H.264（WebCodecs）解码器：画面序号 -> {canvas, decoder, …}
               decoders: new Map(),
               // 服务端下发的设备族表（`GET /api/printers` 的 `families`）：
               // 添加/编辑表单的下拉、凭据标签与端口提示全部按它渲染
               families: []};
const wall = document.getElementById('wall');
const MAGIC_A = 0x42, MAGIC_B = 0x4D, HEADER = 9, KIND_FRAME = 1, KIND_STATUS = 2,
      KIND_H264 = 3;

/* ---------------------------------------------------------------- 令牌 */
function cookieGet(name){
  const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
  return m ? decodeURIComponent(m[1]) : '';
}
function cookieSet(name, value){
  document.cookie = name + '=' + encodeURIComponent(value) + ';path=/;max-age=31536000';
}
function initToken(){
  const fromUrl = new URLSearchParams(location.search).get('token');
  if (fromUrl){ cookieSet('bm_token', fromUrl); state.token = fromUrl; history.replaceState(null, '', location.pathname); }
  else { state.token = cookieGet('bm_token'); }
}
function api(path){
  return path + (path.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(state.token);
}
/* 成功响应里可能带 warning（不阻断操作的提示，例如"本平台没有 DPAPI，
   访问代码以明文保存"）。拼成一句附在成功提示后面 —— 关键是**不能**把它
   当成失败，否则安卓上设备明明加上了却显示"添加失败"。 */
function warnSuffix(result){
  return (result && result.warning) ? '（提示：' + result.warning + '）' : '';
}
function showLogin(show){ document.getElementById('login').style.display = show ? 'flex' : 'none'; }

/* ---------------------------------------------------------------- 时钟 */
function clock(){
  const d = new Date(), p = n => String(n).padStart(2, '0');
  document.getElementById('clock').textContent =
    `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
setInterval(clock, 1000); clock();

/* ---------------------------------------------------------------- 布局 */
document.getElementById('cols').addEventListener('change', e => {
  const v = e.target.value;
  wall.className = v === '0' ? '' : ('cols' + v);
  localStorage.setItem('bm_cols', v);
});
const savedCols = localStorage.getItem('bm_cols');
if (savedCols){ document.getElementById('cols').value = savedCols; wall.className = savedCols === '0' ? '' : ('cols' + savedCols); }
document.getElementById('btn-reload').addEventListener('click', () => location.reload());

/* ------------------------------------------------- 安装到手机主屏（PWA） */
let installPrompt = null;
window.addEventListener('beforeinstallprompt', event => {
  event.preventDefault();
  installPrompt = event;
  document.getElementById('btn-install').style.display = '';
});
document.getElementById('btn-install').addEventListener('click', async () => {
  if (!installPrompt) return;
  installPrompt.prompt();
  try { await installPrompt.userChoice; } catch (err) { /* 忽略 */ }
  installPrompt = null;
  document.getElementById('btn-install').style.display = 'none';
});
if ('serviceWorker' in navigator){
  // Service Worker 只在 HTTPS 或 localhost 下可用；局域网 HTTP 访问时注册会失败，忽略即可，
  // 依然可以通过浏览器菜单「添加到主屏幕」安装。
  navigator.serviceWorker.register('/sw.js').catch(() => undefined);
}
document.getElementById('token-ok').addEventListener('click', () => {
  const value = document.getElementById('token-input').value.trim();
  if (!value) return;
  cookieSet('bm_token', value); state.token = value; location.reload();
});

/* ---------------------------------------------------------------- 画面 */
function ensureTile(index){
  let tile = state.tiles.get(index);
  if (tile) return tile;
  const element = document.createElement('article');
  element.className = 'tile';
  element.innerHTML = `
    <div class="video">
      <img alt="">
      <div class="nosignal">等待画面…</div>
      <div class="novideo" style="display:none"></div>
      <div class="overlay"></div>
      <div class="status"><span class="dot"></span><span class="status-text">连接中</span></div>
    </div>
    <div class="bar">
      <div class="row1">
        <span class="badge">—</span><span class="pct">--%</span>
        <span class="task"></span><span class="remain">剩余 --</span>
      </div>
      <div class="progress"><i></i></div>
      <div class="row2">
        <span>喷嘴 <b class="nozzle">--</b>/<span class="nozzle-t">--</span>℃</span>
        <span>热床 <b class="bed">--</b>/<span class="bed-t">--</span>℃</span>
        <span class="chamber"></span>
        <span class="layer"></span><span class="err"></span>
      </div>
      <div class="filament"></div>
      <div class="row4">
        <span class="ctrl-hint" style="display:none"></span>
        <span class="wifi"></span>
        <span class="finish"></span>
        <span class="hms" style="display:none"></span>
        <span class="grow"></span>
        <button data-act="pause">⏸ 暂停</button>
        <button data-act="stop">⏹ 停止</button>
        <button data-act="light">💡 灯</button>
      </div>
      <details class="details" style="display:none">
        <summary>详情</summary>
        <table><tbody></tbody></table>
      </details>
    </div>`;
  const img = element.querySelector('img');
  const nosignal = element.querySelector('.nosignal');
  img.addEventListener('load', () => {
    nosignal.style.display = 'none';
    const previous = img.dataset.previous;
    if (previous){ URL.revokeObjectURL(previous); img.dataset.previous = ''; }
  });
  img.addEventListener('error', () => {
    nosignal.style.display = 'flex';
    nosignal.textContent = '画面断开，正在重试…';
    if (state.fallback){
      setTimeout(() => { img.src = api('/stream/' + index) + '&t=' + Date.now(); }, 3000);
    }
  });
  element.addEventListener('click', () => {
    // 长按刚弹过菜单：这次点击是长按的尾巴，不要再切换全屏
    if (element.dataset.menuJustOpened){ delete element.dataset.menuJustOpened; return; }
    element.classList.toggle('full');
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') element.classList.remove('full'); });
  // 右键（触屏长按）菜单：重连 / 抓拍 / 复制 IP / 编辑 / 删除
  bindTileMenu(element, index);
  element.querySelectorAll('.row4 button').forEach(button => {
    button.addEventListener('click', event => {
      event.stopPropagation();
      const action = button.dataset.act;
      const status = tile.status || {};
      const map = {pause: status.paused ? 'resume' : 'pause', stop: 'stop', light: 'light_toggle'};
      sendCommand(index, map[action] || action);
    });
  });
  const hmsBadge = element.querySelector('.hms');
  hmsBadge.addEventListener('click', event => { event.stopPropagation(); showHms(index); });
  // 「详情」默认收起，点 summary 展开时这次点击**不能**冒泡到上面那个
  // 「点整卡切换全屏」的监听 —— 否则用户想看读数，画面却直接全屏了。
  element.querySelector('.details').addEventListener('click', event => event.stopPropagation());
  tile = {element, img, nosignal, status: {}};
  state.tiles.set(index, tile);
  wall.appendChild(element);
  return tile;
}

/* ---------------------------------------------------------------- 指令 */
function toast(message){
  const box = document.getElementById('toast');
  box.textContent = message;
  box.style.display = 'block';
  clearTimeout(box._timer);
  box._timer = setTimeout(() => { box.style.display = 'none'; }, 3500);
}

async function sendCommand(index, action, value){
  const names = {pause:'暂停打印', resume:'继续打印', stop:'停止打印', light_toggle:'开关舱灯'};
  if (action === 'stop' && !confirm('确定要停止这台打印机的打印任务吗？\\n\\n停止后无法继续，需要重新开始打印。')){
    return;
  }
  try{
    const response = await fetch(api('/api/command'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({index: index, action: action, value: value || ''})
    });
    if (response.status === 401){ showLogin(true); return; }
    const data = await response.json();
    toast(data.ok ? (data.detail || (names[action] + '已发送')) : ('指令失败：' + (data.detail || '')));
  }catch(err){
    toast('指令发送失败（网络异常）');
  }
}

function showHms(index){
  const tile = state.tiles.get(index);
  const items = (tile && tile.status && tile.status.hms) || [];
  const box = document.getElementById('hmsbox');
  if (!items.length){ box.style.display = 'none'; return; }
  const name = (tile.status && tile.status.name) || '';
  box.innerHTML = `<div class="card"><b>${name} · HMS 提示（${items.length} 条）</b></div>` +
    items.map(item => `<div class="card"><div style="color:var(--err);font-weight:700">${item.code || ''}</div>` +
      `<div style="margin:6px 0">${item.text || ''}</div>` +
      `<a href="${item.wiki}" target="_blank" rel="noreferrer">查看官方说明 →</a></div>`).join('') +
    `<div class="card" style="text-align:center"><button onclick="document.getElementById('hmsbox').style.display='none'">关闭</button></div>`;
  box.style.display = 'block';
}

function applyFrame(index, payload){
  const tile = ensureTile(index);
  const blob = new Blob([payload], {type: 'image/jpeg'});
  const url = URL.createObjectURL(blob);
  const img = tile.img;
  img.dataset.previous = img.dataset.current || '';
  img.dataset.current = url;
  img.src = url;
}

/* ---------------------------------------------------------------- 设备详情
   第三方族（Klipper/Moonraker）能给出十几条读数：风扇转速、断料/走料传感器、
   工具头板温度、耗材用量、文件位置、主机负载…这些过去取到了却没有任何界面显示。
   服务端把它们放在每台设备的 `details` 里（`[{"label": …, "value": …}, …]`）。

   为什么默认收起（<details>）：18 条读数全铺开时卡片会比画面还高，一屏就被读数
   占满 —— 画面才是主角，读数按需展开。

   为什么重建前先比签名：applyStatus() 每一帧都会走到这里，无脑清空重建会让用户
   正在看的列表闪烁。<details> 的展开状态是 DOM 属性，**不重建就不会被重置**，
   所以刷新时绝不碰 `.open`：否则用户展开的详情每次刷新都会被收起来。

   为什么只用 createElement + textContent：读数是设备/用户数据，拼 innerHTML
   等于把设备返回的字符串当代码渲染。 */
function renderDetails(element, info){
  const box = element.querySelector('.details');
  if (!box) return;
  const rows = Array.isArray(info.details) ? info.details : [];
  // 空列表 = 这台设备没有这类读数（拓竹那族就是空列表）：整块隐藏，不留空框、不占行高
  if (!rows.length){
    box.style.display = 'none';
    return;
  }
  const signature = JSON.stringify(rows.map(row => [row.label, row.value]));
  if (box.dataset.signature !== signature){
    box.dataset.signature = signature;
    const body = box.querySelector('tbody');
    body.textContent = '';
    rows.forEach(row => {
      const tr = document.createElement('tr');
      const key = document.createElement('td');
      key.className = 'k';
      key.textContent = row.label || '';
      const value = document.createElement('td');
      value.className = 'v';
      value.textContent = row.value == null ? '' : String(row.value);
      tr.appendChild(key);
      tr.appendChild(value);
      body.appendChild(tr);
    });
    // 摘要带上条数：用户一眼就知道值不值得展开
    box.querySelector('summary').textContent = '详情（' + rows.length + ' 条）';
  }
  box.style.display = '';
}

function applyStatus(data){
  const printers = data.printers || [];
  // 记下来给右键菜单用（菜单要知道当前是哪台设备）
  state.lastPrinters = printers;
  // 顺带刷新设备族表：添加表单据此渲染（服务端注册表改了不必重载页面）
  cacheFamilies(data);
  printers.forEach((info, index) => {
    const tile = ensureTile(index);
    tile.status = info;
    const element = tile.element;
    element.querySelector('.overlay').textContent = `${info.name} · ${info.ip}`;
    element.style.gridColumn = info.span > 1 ? `span ${info.span}` : '';
    element.style.gridRow = info.span > 1 ? `span ${info.span}` : '';
    const color = info.camera_online && info.mqtt_online ? 'var(--ok)'
                : (info.camera_online || info.mqtt_online) ? 'var(--warn)' : 'var(--err)';
    element.querySelector('.dot').style.background = color;
    const statusNode = element.querySelector('.status-text');
    statusNode.textContent = info.status_text;
    // 完整原因（例如「RTSPS(322) 未取到画面…」）放到悬浮提示里：
    // 状态行位置很窄，直接显示会被截断成看不懂的半句话
    statusNode.title = info.status_detail || info.status_text;

    const badge = element.querySelector('.badge');
    badge.textContent = info.state_text;
    const badgeColor = {'打印中':'var(--accent)','准备中':'var(--accent)','已暂停':'var(--warn)',
                        '打印完成':'var(--ok)','打印失败':'var(--err)','空闲':'var(--dim)','离线':'var(--err)'}[info.state_text] || 'var(--dim)';
    badge.style.color = badgeColor; badge.style.borderColor = badgeColor;

    element.querySelector('.pct').textContent = info.progress + '%';
    element.querySelector('.progress i').style.width = Math.max(0, Math.min(100, info.progress)) + '%';
    element.querySelector('.task').textContent = info.task || '';
    element.querySelector('.remain').textContent = '剩余 ' + info.remaining_text;
    element.querySelector('.nozzle').textContent = info.nozzle;
    element.querySelector('.nozzle-t').textContent = info.nozzle_target;
    element.querySelector('.bed').textContent = info.bed;
    element.querySelector('.bed-t').textContent = info.bed_target;
    element.querySelector('.chamber').textContent = info.chamber ? ('仓温 ' + info.chamber + '℃') : '';
    element.querySelector('.layer').textContent = info.layer_text || '';
    element.querySelector('.err').textContent = info.problem_text || '';

    // WiFi 信号（▮▮▮▯）
    const level = Math.max(0, Math.min(4, info.wifi_level || 0));
    element.querySelector('.wifi').textContent =
      info.wifi ? ('WiFi ' + '▮'.repeat(level) + '▯'.repeat(4 - level) + ' ' + info.wifi) : '';
    element.querySelector('.finish').textContent =
      info.finish_time && info.finish_time !== '--' ? ('预计完成 ' + info.finish_time) : '';

    // 耗材（AMS + 外挂）
    const filament = element.querySelector('.filament');
    const trays = (info.ams || []).slice();
    if (info.external) trays.push(info.external);
    const signature = JSON.stringify(trays.map(t => [t.label, t.color, t.type, t.remain_text])) + info.active_tray;
    if (filament.dataset.signature !== signature){
      filament.dataset.signature = signature;
      filament.innerHTML = trays.length ? '<span>耗材</span>' + trays.map(t => {
        const active = t.label === info.active_tray ? ' active' : '';
        // 余量读不到时（非官方料卷 / 外挂）不显示，避免误导
        const remain = t.remain_text ? (' ' + t.remain_text) : '';
        const title = t.remain_hint || '';
        return `<span class="chip${active}" title="${title}"><i class="sw" style="background:${t.color || '#3a3a3a'}"></i>` +
               `${t.label} ${t.type || '--'}${remain}</span>`;
      }).join('') : '';
    }

    // 设备详情（第三方族的额外读数）：默认收起，展开状态跨刷新保留
    renderDetails(element, info);

    // HMS
    const hmsBadge = element.querySelector('.hms');
    const hmsCount = (info.hms || []).length;
    if (hmsCount){
      hmsBadge.textContent = '⚠ HMS×' + hmsCount;
      hmsBadge.style.display = '';
    } else {
      hmsBadge.style.display = 'none';
    }

    // 控制按钮
    const pauseButton = element.querySelector('button[data-act="pause"]');
    const stopButton = element.querySelector('button[data-act="stop"]');
    const lightButton = element.querySelector('button[data-act="light"]');
    pauseButton.textContent = info.paused ? '▶ 继续' : '⏸ 暂停';
    pauseButton.style.display = info.printing ? '' : 'none';
    pauseButton.disabled = !info.can_control;
    stopButton.style.display = info.printing ? '' : 'none';
    stopButton.disabled = !info.can_control;
    lightButton.textContent = info.light === 'on' ? '💡 关灯' : '💡 开灯';
    // 灯按钮：设备**上报了灯**（light 为 'on'/'off'）或能力声明有灯时才显示。
    // 与桌面端保持同一判据 —— 机型规格推不出灯光能力，只有设备上报才算数。
    const hasLight = info.light === 'on' || info.light === 'off' ||
                     (info.capabilities && info.capabilities.light);
    lightButton.style.display = hasLight ? '' : 'none';
    lightButton.disabled = !info.can_control;
    // 控制被固件签名要求挡住时，把原因挂到提示上：
    // 按钮是灰的却不说明原因，用户会以为软件坏了
    const blocked = info.controls_blocked_reason || '';
    [pauseButton, stopButton, lightButton].forEach(button => {
      button.title = blocked;
    });
    // 触屏看不到 tooltip，所以在状态条里显示一条可点击的提示条。
    // 这条提示是"用户能自己解决问题"的关键：直接告诉他去打印机上放行。
    //
    // ⚠️ 两条文案要求（都有回归测试盯着）：
    //   1. 必须**逐条命令**说清楚，不能笼统写"控制不可用" ——
    //      固件签名只覆盖 print 段（暂停/停止/速度），灯控走 system 段
    //      **不受影响**。曾经统一显示「⚠ 控制不可用」，结果安卓用户看到
    //      "控制不可用"却发现灯明明能开关，提示与事实矛盾，比不提示更糟。
    //   2. 必须点出**怎么放行**（局域网模式 / 开发者模式），
    //      否则用户只知道"不能用"却不知道去哪儿改。
    const hint = element.querySelector('.ctrl-hint');
    if (blocked){
      // 与后端 SIGNATURE_SENSITIVE_COMMANDS 一致：哪些命令真的被挡
      const blockedActs = (info.printing ? ['pause', 'stop'] : [])
        .filter(act => {
          const button = element.querySelector('button[data-act="' + act + '"]');
          return button && button.style.display !== 'none';
        });
      // 窄位置放不下整段说明，但必须带上「先局域网、再开发者」与「农场管家」：
      //   * 只开局域网模式**没用** —— 画面遥测会通，暂停/停止仍被固件忽略，
      //     用户会以为软件坏了（实测结论，见 docs/FIELD_NOTES.md）；
      //   * 灯控不受影响，所以文案不能说成"控制全不可用"。
      const short = info.controls_blocked_short || '';
      if (blockedActs.length > 0){
        hint.textContent = '⚠ ' + blockedActs.length + ' 项被固件挡住（开关灯不受影响）：' +
          short + '（点此查看做法）';
      } else {
        // 当前没有被挡住的可见控制（例如空闲时没有暂停/停止按钮），
        // 此时不该报"控制不可用" —— 灯控等仍然可用
        hint.textContent = 'ℹ ' + (short || '暂停/停止/速度被固件挡住') +
          '；开关灯不受影响（点此查看做法）';
      }
      hint.style.display = '';
      hint.onclick = (event) => {
        event.stopPropagation();
        alert(blocked);
      };
    } else {
      hint.style.display = 'none';
      hint.onclick = null;
    }

    if (!state.live && !info.camera_online){
      tile.img.style.visibility = 'hidden';
    } else {
      tile.img.style.visibility = 'visible';
    }

    // 「这台设备在本机根本出不了画面」时，把原因**直接写在画面上**：
    // 典型是安卓版 + 只支持 RTSPS(322) 的机型（X1/X2D/H2/P2S）—— APK 为兼容
    // 16KB 内存页设备刻意不内置 OpenCV，所以这类机型在平板上永远没有画面。
    // 只把它塞进 tooltip 是不够的：触屏根本看不到悬浮提示，用户只会看到
    // 一个永远空着的画面和一句「连接中」，以为软件坏了。
    const noVideo = info.video_unavailable_reason || '';
    const note = tile.element.querySelector('.novideo');
    if (noVideo){
      note.textContent = '⚠ ' + noVideo;
      note.style.display = '';
    } else {
      note.style.display = 'none';
    }
  });
  for (const [index, tile] of [...state.tiles]){
    if (index >= printers.length){ tile.element.remove(); state.tiles.delete(index); }
  }
  document.getElementById('summary').textContent =
    `共 ${data.total} 台 · 画面在线 ${data.camera_online} · 遥测在线 ${data.mqtt_online} · 打印中 ${data.printing}`;
}

/* ------------------------------------------------- 单连接多路复用（实时模式） */
function concatBuffer(buffer, chunk){
  const merged = new Uint8Array(buffer.length + chunk.length);
  merged.set(buffer, 0);
  merged.set(chunk, buffer.length);
  return merged;
}

function processBuffer(buffer){
  let offset = 0;
  while (true){
    // 同步到魔数
    while (offset + 1 < buffer.length && !(buffer[offset] === MAGIC_A && buffer[offset + 1] === MAGIC_B)) offset++;
    if (offset + HEADER > buffer.length) break;
    const kind = buffer[offset + 2];
    const index = buffer[offset + 3] | (buffer[offset + 4] << 8);
    const length = (buffer[offset + 5] | (buffer[offset + 6] << 8) |
                    (buffer[offset + 7] << 16) | (buffer[offset + 8] << 24)) >>> 0;
    if (offset + HEADER + length > buffer.length) break;
    const payload = buffer.slice(offset + HEADER, offset + HEADER + length);
    offset += HEADER + length;
    if (kind === KIND_FRAME){
      applyFrame(index, payload);
    } else if (kind === KIND_H264){
      applyH264(index, payload);
    } else if (kind === KIND_STATUS){
      try { applyStatus(JSON.parse(new TextDecoder().decode(payload))); } catch (err) { /* 忽略坏包 */ }
    }
  }
  let rest = buffer.slice(offset);
  if (rest.length > 16 * 1024 * 1024) rest = new Uint8Array(0);  // 防御：异常数据不要无限增长
  return rest;
}

/* ------------------------------------------------------------ H.264（WebCodecs）

   为什么有这条通路：**安卓版没有 OpenCV**。APK 刻意不打包 opencv/numpy
   （Chaquopy 的轮子是 4096 字节对齐，16KB 内存页设备会拒绝加载 → 闪退），
   而只提供 RTSPS(322) 通道的机型（X1/X1C/X2D/H2/P2S）想拿画面就必须解码 H.264。
   服务端于是**不解码**，只把码流（AVCC 格式）推过来，由这里的 WebCodecs
   （Android WebView / Chrome 都自带硬件解码器）解出来画到 canvas 上。

   数据格式（KIND_H264 的负载）：首字节 0x01 = 初始化参数(JSON)
   （codec 串 + avcC description 的 base64），0x02 = 增量帧，0x03 = 关键帧。
*/
function decoderSupported(){
  return typeof window.VideoDecoder === 'function';
}

function ensureDecoder(index){
  let entry = state.decoders.get(index);
  if (entry) return entry;
  const tile = state.tiles.get(index);
  if (!tile) return null;
  // 画面容器：优先复用 <img> 所在的 .video 区，换成 canvas
  const canvas = document.createElement('canvas');
  canvas.style.width = '100%';
  canvas.style.height = '100%';
  canvas.style.objectFit = 'contain';
  canvas.style.display = 'block';
  tile.img.parentNode.insertBefore(canvas, tile.img);
  tile.img.style.display = 'none';
  entry = {canvas, decoder: null, configured: false, pending: [], codec: '', created: 0,
           index};
  state.decoders.set(index, entry);
  return entry;
}

function applyH264(index, payload){
  if (!decoderSupported()){
    showH264Unsupported(index);
    return;
  }
  if (payload.length < 1) return;
  const kind = payload[0];
  const body = payload.slice(1);
  const entry = ensureDecoder(index);
  if (!entry) return;

  if (kind === 0x01){
    let params;
    try { params = JSON.parse(new TextDecoder().decode(body)); }
    catch (err) { return; }
    configureDecoder(index, entry, params);
    return;
  }

  if (!entry.configured){
    // 初始化参数还没到：先攒几帧，等 configure 之后再喂（WebCodecs 要求先配置）
    if (entry.pending.length < 60) entry.pending.push({key: kind === 0x03, data: body});
    return;
  }
  decodeChunk(entry, kind === 0x03, body);
}

function configureDecoder(index, entry, params){
  const codec = params.codec || '';
  const description = params.description ? base64ToBytes(params.description) : null;
  // codec 换了（换了机型/固件）：重建解码器
  if (entry.decoder && entry.codec === codec){
    entry.configured = true;
    return;
  }
  try {
    if (entry.decoder){ try { entry.decoder.close(); } catch (err) {} }
    entry.decoder = new VideoDecoder({
      output: frame => drawFrame(index, frame),
      error: err => { h264Failed(index, '解码器错误：' + err); },
    });
    const config = {codec, optimizeForLatency: true};
    if (description){ config.description = description; }
    entry.decoder.configure(config);
    entry.codec = codec;
    entry.configured = true;
    const queued = entry.pending; entry.pending = [];
    queued.forEach(item => decodeChunk(entry, item.key, item.data));
  }catch(err){
    entry.configured = false;
    h264Failed(index, '该浏览器无法解码 ' + codec + '（' + err + '）');
  }
}

function decodeChunk(entry, isKey, data){
  if (!entry.decoder || entry.decoder.state !== 'configured') return;
  // 队列太长说明解码跟不上：丢掉增量帧，只保留关键帧（否则延迟越积越大）
  if (entry.decoder.decodeQueueSize > 20 && !isKey) return;
  try {
    entry.decoder.decode(new EncodedVideoChunk({
      type: isKey ? 'key' : 'delta',
      timestamp: entry.created++ * 40000,   // 单调递增即可（微秒）
      data,
    }));
  }catch(err){
    h264Failed(entry.index, '解码失败：' + err);
  }
}

function drawFrame(index, frame){
  const entry = state.decoders.get(index);
  if (!entry) { frame.close(); return; }
  const canvas = entry.canvas;
  if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight){
    canvas.width = frame.displayWidth;
    canvas.height = frame.displayHeight;
  }
  try {
    canvas.getContext('2d').drawImage(frame, 0, 0, canvas.width, canvas.height);
    const tile = state.tiles.get(index);
    if (tile) tile.nosignal.style.display = 'none';
  }catch(err){ /* 画不出来就算了，下一帧还有机会 */ }
  frame.close();
}

function h264Failed(index, message){
  const tile = state.tiles.get(index);
  if (!tile) return;
  tile.nosignal.style.display = 'flex';
  tile.nosignal.textContent = message;
}

/** 浏览器不支持 WebCodecs 时给出的说明（这是唯一真正无解的情形）。 */
function showH264Unsupported(index){
  const tile = state.tiles.get(index);
  if (!tile || tile._h264Unsupported) return;
  tile._h264Unsupported = true;
  tile.nosignal.style.display = 'flex';
  tile.nosignal.textContent = '这台设备的浏览器不支持 WebCodecs（需要 WebView 94+）：' +
    '该机型只有 RTSPS(322) 画面通道，无法在此解码。用电脑版或服务端网页看画面即可。';
}

function base64ToBytes(text){
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

async function startLive(){
  if (state.fallback) return;
  try{
    const response = await fetch(api('/api/live'), {cache: 'no-store'});
    if (response.status === 401){ showLogin(true); return; }
    if (!response.ok || !response.body || !response.body.getReader){
      throw new Error('该浏览器不支持流式读取');
    }
    showLogin(false);
    state.live = true;
    state.liveFails = 0;
    document.getElementById('mode').textContent = '实时通道（单连接多路）';
    const reader = response.body.getReader();
    let buffer = new Uint8Array(0);
    while (true){
      const {value, done} = await reader.read();
      if (done) break;
      buffer = concatBuffer(buffer, value);
      buffer = processBuffer(buffer);
    }
  }catch(err){
    state.liveFails += 1;
    if (state.liveFails >= 2){
      // 连续失败：退回逐路 MJPEG + 轮询（受浏览器 6 连接限制，画面多时会有排队）
      state.fallback = true;
      state.live = false;
      document.getElementById('mode').textContent = '兼容模式（逐路 MJPEG）';
      for (const [index, tile] of state.tiles){
        tile.img.src = api('/stream/' + index) + '&t=' + Date.now();
        tile.img.style.visibility = 'visible';
      }
      setInterval(pollStatus, 1500);
      return;
    }
  }
  state.live = false;
  document.getElementById('mode').textContent = '正在重连…';
  setTimeout(startLive, 1500);
}

async function pollStatus(){
  try{
    const response = await fetch(api('/api/printers'), {cache: 'no-store'});
    if (response.status === 401){ showLogin(true); return; }
    applyStatus(await response.json());
  }catch(err){ /* 忽略，下一轮再试 */ }
}

initToken();
if (state.token === '') showLogin(true);
startLive();
setTimeout(() => { if (!state.live && !state.fallback) pollStatus(); }, 1200);

/* ============================================================================
   设备管理与设置

   桌面端有「自动搜索 / 添加打印机 / 画面布局 / 设置」这些对话框；网页端原先
   只能看不能管。**安卓版没有桌面界面，网页是唯一入口**，所以这些能力必须补齐，
   否则平板用户没法把打印机加进来。

   实现要点：服务端把「发现 / 添加 / 删除 / 重连 / 设置」暴露成回调注入的 API
   （见 app/web/server.py），宿主（安卓版、桌面版、无界面版）各自提供实现。
   后端不支持时返回 501，这里据此给出提示而不是静默失败。
   ========================================================================= */

const modal = document.getElementById('modal');
const modalCard = document.getElementById('modal-card');
const menu = document.getElementById('menu');

function closeModal(){ modal.style.display = 'none'; modalCard.innerHTML = ''; }
function closeMenu(){ menu.style.display = 'none'; }
modal.addEventListener('click', event => { if (event.target === modal) closeModal(); });
document.addEventListener('click', event => {
  if (!menu.contains(event.target)) closeMenu();
});
document.addEventListener('keydown', event => { if (event.key === 'Escape'){ closeMenu(); closeModal(); } });

/** 统一的 POST 助手：把服务端的 {ok, detail} 约定处理成一句话。 */
async function postJson(path, payload){
  try{
    const response = await fetch(api(path), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload || {})
    });
    if (response.status === 401){ showLogin(true); return {ok:false, detail:'未授权'}; }
    if (response.status === 501){
      return {ok:false, detail:'该运行方式不支持此操作（需要用桌面版或无界面版）'};
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok && !data.detail){ data.detail = 'HTTP ' + response.status; }
    return data;
  }catch(err){
    return {ok:false, detail:'请求失败：' + err};
  }
}

/* ---------------------------------------------------------------- 自动搜索 */
async function openDiscover(){
  modalCard.innerHTML =
    '<h2>自动搜索局域网打印机</h2>' +
    '<div class="hint">会同时用 SSDP 组播与旧版广播扫描，约 10-20 秒。<br>' +
    '搜索到后填上访问代码（打印机屏幕 → 设置 → 网络 → 局域网访问代码）再添加。</div>' +
    '<div id="disc-status" class="status">正在扫描…</div>' +
    '<div id="disc-list" class="list"></div>' +
    '<div class="actions">' +
      '<button id="disc-again">重新扫描</button>' +
      '<button id="disc-close">关闭</button>' +
    '</div>';
  modal.style.display = 'flex';
  document.getElementById('disc-close').addEventListener('click', closeModal);
  document.getElementById('disc-again').addEventListener('click', runDiscover);
  runDiscover();
}

async function runDiscover(){
  const status = document.getElementById('disc-status');
  const list = document.getElementById('disc-list');
  status.textContent = '正在扫描（约 10-20 秒）…';
  list.innerHTML = '';
  let data;
  try{
    const response = await fetch(api('/api/discover'), {cache:'no-store'});
    if (response.status === 501){
      status.textContent = '该运行方式不支持自动搜索，请用「＋ 添加」手动填写 IP。';
      return;
    }
    if (response.status === 401){ showLogin(true); return; }
    data = await response.json();
  }catch(err){
    status.textContent = '扫描失败：' + err;
    return;
  }
  if (data.error){ status.textContent = '扫描出错：' + data.error; return; }
  const found = data.printers || [];
  if (!found.length){
    status.textContent = '没有发现设备。请确认打印机已开机、与本站同一网段；' +
                         '跨 VLAN 时请用「＋ 添加」手动填 IP。';
    return;
  }
  status.textContent = '发现 ' + found.length + ' 台（带「已添加」标记的已在监控墙上）';
  found.forEach(item => {
    const row = document.createElement('div');
    row.className = 'item';
    row.innerHTML =
      '<span class="name">' + escapeHtml(item.name || item.model || item.ip) +
      '<div class="meta">' + escapeHtml(item.ip) + ' · ' + escapeHtml(item.model || '未知机型') +
      (item.serial ? ' · ' + escapeHtml(item.serial) : '') + '</div></span>' +
      (item.known ? '<span class="tag">已添加</span>' : '');
    const code = document.createElement('input');
    code.placeholder = '访问代码';
    code.style.flex = '0 0 120px';
    code.autocomplete = 'off';
    const add = document.createElement('button');
    add.textContent = item.known ? '更新' : '添加';
    add.className = 'primary';
    add.addEventListener('click', async () => {
      add.disabled = true;
      const result = await postJson('/api/add_printer', {
        name: item.name || '', ip: item.ip,
        access_code: code.value.trim(), model: item.model || '',
        // 序列号必须带上：遥测靠它订阅 device/<序列号>/report，
        // 缺了会导致「有画面但永远没有进度/温度」
        serial: item.serial || ''
      });
      // 注意 result.warning：像「本平台没有 DPAPI，凭据按明文保存」这类提示
      // 不影响添加结果，但要让用户知道。之前把提示当错误，安卓上会报
      // 「添加失败当前系统没有 DPAPI」—— 其实设备已经加上了。
      toast(result.ok ? (result.detail || '已添加') + warnSuffix(result)
                      : ('添加失败：' + (result.detail || '')));
      if (result.ok){ row.querySelector('.tag') || row.appendChild(makeTag('已添加')); }
      add.disabled = false;
    });
    row.appendChild(code);
    row.appendChild(add);
    list.appendChild(row);
  });
}

function makeTag(text){
  const tag = document.createElement('span');
  tag.className = 'tag';
  tag.textContent = text;
  return tag;
}
function escapeHtml(text){
  return String(text == null ? '' : text).replace(/[&<>"']/g,
    ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}

/* ---------------------------------------------------------------- 设备族
   设备族（拓竹 / Klipper-Moonraker / …）由服务端下发：`GET /api/printers` 顶层的
   `families` 里带着每个族的展示名、凭据策略（字段名 / 标签 / 提示 / 是否必填）、
   默认端口与候选端口。表单据此**动态渲染**，前端不写死任何族独有的文案 ——
   以后服务端登记新族（见 app/core/registry.py），网页与安卓端不用改一行代码。

   唯一写死的是族的 **id**：服务端约定「family 为空 = 拓竹」，前端需要用它决定
   下拉的默认项，以及「只有第三方族才需要端口 / 摄像头 URL」。 */
const FAMILY_BAMBU = 'bambu';

/** 记住最近一次下发的设备族表（切对话框、切族时不重复请求）。 */
function cacheFamilies(data){
  const list = data && data.families;
  // 只覆盖非空值：别的接口没有 families 键，不能把已有的表清掉
  if (Array.isArray(list) && list.length) state.families = list;
}

/** 取一个族的描述；表里没有就返回 null（老服务端可能根本不带这张表）。 */
function familyOf(id){
  return (state.families || []).find(item => item.family === (id || '')) || null;
}

/** 默认族：拓竹。表里万一没有拓竹，就退回第一项，再不行用空串（服务端把空当拓竹）。 */
function defaultFamilyId(){
  const list = state.families || [];
  const bambu = list.find(item => item.family === FAMILY_BAMBU);
  return (bambu || list[0] || {family: ''}).family;
}

/** 是不是第三方族：拓竹族的端口与画面地址由服务端自动发现，表单不用管这两个。 */
function isThirdPartyFamily(id){ return (id || '') !== FAMILY_BAMBU; }

/** 凭据策略（该族凭据填在哪个字段、叫什么、是否必填、去哪儿拿）。
    老服务端没有 families 时给一份中性兜底：服务端本身仍把空 family 当拓竹。 */
function credentialPolicy(id){
  const descriptor = familyOf(id);
  const policy = descriptor && descriptor.credential;
  if (policy && (policy.label || policy.key)) return policy;
  return {key: 'access_code', label: '凭据', required: false, hint: ''};
}

/** 打开表单前保证拿到设备族表：`/api/printers` 是唯一来源（服务端注册表原样透出）。 */
async function ensureFamilies(){
  if ((state.families || []).length) return state.families;
  try{
    const response = await fetch(api('/api/printers'), {cache:'no-store'});
    if (response.status === 401){ showLogin(true); return state.families || []; }
    cacheFamilies(await response.json());
  }catch(err){ /* 读不到就按老服务端的样子渲染（只能当拓竹），别把添加流程堵死 */ }
  return state.families || [];
}

/** 「设备族」下拉的选项：值是族 id，文字是服务端给的展示名。 */
function familyOptionsHtml(selected){
  return (state.families || []).map(item =>
    '<option value="' + escapeHtml(item.family) + '"' + (item.family === selected ? ' selected' : '') +
    '>' + escapeHtml(item.label || item.family) + '</option>').join('');
}

/** 端口提示里的族信息片段：默认端口与候选端口都来自族数据，
    前端不写死 8883 / 7125 这类数字 —— 服务端加族时提示自动跟着变。 */
function portFamilyDetail(id){
  const descriptor = familyOf(id) || {};
  const parts = [];
  if (descriptor.default_port) parts.push('默认 ' + descriptor.default_port);
  const candidates = descriptor.candidate_ports || [];
  if (candidates.length) parts.push('候选 ' + candidates.join(' / '));
  return parts.join('；');
}

/** 这台设备属于哪个族：服务端在**逐台**载荷里回了 `family`（bambu / moonraker …），
    直接用它。以前是靠 `model` 反查族的展示名（第三方族的 model 就是族名），
    那条路已经删掉：拓竹的机型名万一撞上某个族的展示名就会判错族，
    而判错族的后果是凭据被写进错字段（服务端把它当"没填"直接丢掉）。
    万一拿不到 `family`（理论上不会：页面与接口来自同一个进程），
    按服务端的约定退回默认族 —— 即「family 为空 = 拓竹」。 */
function itemFamilyId(item){
  return (item && item.family) || defaultFamilyId();
}

/** 编辑表单的凭据标签：优先用服务端逐台回传的 `credential_label`（该族对凭据的叫法），
    拿不到时才用 families 表里的 `credential.label`，最后才退化成中性「凭据」——
    三处都不写死「访问代码 / API Key」这类族特有文案。 */
function itemCredentialLabel(item, family){
  return (item && item.credential_label) || credentialPolicy(family).label;
}

/* 切换族时凭据**按族各自保留**：拓竹的访问代码与第三方族的 API Key 语义完全不同，
   共用一个值很容易切来切去把 A 的凭据提交给 B（必然连不上），所以每族存一份草稿。 */
const addCredDrafts = {};
let addCredFamilyId = '';

/** 把当前选中的族应用到「添加」表单：凭据的标签 / 占位提示 / 必填标记，
    以及端口与摄像头 URL 的可见性。

    端口与摄像头 URL 只对第三方族有意义，拓竹族时**整块隐藏**而不是禁用，理由：
      * 默认族就是拓竹，隐藏后表单长度与改造前一致，老用户的操作路径零变化；
      * 安卓/平板窄屏上，两个永远不能填的灰框白占两行，还没法用一句通用文案说清原因；
      * 用的是同一份 DOM，切到第三方族立刻展开，已填的值**不清空**（切回来不丢，
        与凭据按族存草稿的策略一致）。 */
function applyAddFamily(){
  const family = document.getElementById('add-family').value;
  const policy = credentialPolicy(family);
  // 标签与「是否必填」都跟着族走：拓竹必填、Moonraker 内网可留空
  document.getElementById('add-cred-label').textContent =
    policy.label + (policy.required ? '（必填）' : '（可留空）');
  // 占位提示同样来自族数据：每个族「去哪儿拿凭据」的说法不一样
  document.getElementById('add-cred').placeholder = policy.hint || policy.label;
  const detail = portFamilyDetail(family);
  document.getElementById('add-port-hint').textContent =
    '0 或留空 = 用该族默认端口' + (detail ? ('（' + detail + '）') : '');
  const defaultPort = (familyOf(family) || {}).default_port;
  document.getElementById('add-port').placeholder = defaultPort ? String(defaultPort) : '0';
  document.getElementById('add-thirdparty').style.display =
    isThirdPartyFamily(family) ? '' : 'none';
}

/* ---------------------------------------------------------------- 手动添加 */
async function openAdd(){
  // 设备族表必须先拿到，否则下拉是空的：/api/printers 是唯一来源
  modalCard.innerHTML = '<h2>添加打印机</h2><div class="hint">正在读取设备族…</div>';
  modal.style.display = 'flex';
  await ensureFamilies();
  const selected = defaultFamilyId();
  modalCard.innerHTML =
    '<h2>添加打印机</h2>' +
    '<div class="hint">先选设备族，再填 IP 与凭据就能显示画面。' +
    '<b>序列号建议一并填写</b>：进度、温度这些遥测数据靠它订阅，' +
    '缺了会出现「有画面但没有进度/温度」。在打印机屏幕上：设置 → 设备信息。</div>' +
    '<label>设备族</label><select id="add-family">' + familyOptionsHtml(selected) + '</select>' +
    '<label>名称（可留空）</label><input id="add-name" placeholder="例如 车间 X2D">' +
    '<label>IP 地址</label><input id="add-ip" placeholder="192.168.1.50" inputmode="decimal">' +
    // 凭据的标签/占位/必填标记由 applyAddFamily() 按所选族写入，这里只写中性占位文案
    '<label id="add-cred-label">凭据</label><input id="add-cred" autocomplete="off">' +
    '<label>序列号（建议填写，用于遥测）</label>' +
    '<input id="add-serial" placeholder="例如 01P00A123456789" autocomplete="off">' +
    '<label>机型（可留空，自动识别）</label><input id="add-model" placeholder="例如 X2D">' +
    // 端口与摄像头 URL 只对第三方族有意义 → 放进可整块隐藏的容器（见 applyAddFamily）
    '<div id="add-thirdparty">' +
      '<label>端口（可留空）</label>' +
      '<input id="add-port" type="number" min="0" max="65535" inputmode="numeric" placeholder="0">' +
      '<div class="hint" id="add-port-hint"></div>' +
      '<label>摄像头 URL（可留空）</label>' +
      '<input id="add-camera" placeholder="留空 = 自动发现" autocomplete="off">' +
    '</div>' +
    '<div class="status" id="add-status"></div>' +
    '<div class="actions"><button id="add-cancel">取消</button>' +
    '<button id="add-ok" class="primary">添加</button></div>';
  modal.style.display = 'flex';
  // 把上次填过的凭据放回来（addCredDrafts 按族存，关掉对话框再打开也不丢）
  addCredFamilyId = selected;
  document.getElementById('add-cred').value = addCredDrafts[addCredFamilyId] || '';
  applyAddFamily();
  document.getElementById('add-family').addEventListener('change', event => {
    // 先把当前输入存回旧族，再取新族的草稿 —— 来回切都不会丢
    addCredDrafts[addCredFamilyId] = document.getElementById('add-cred').value;
    addCredFamilyId = event.target.value;
    document.getElementById('add-cred').value = addCredDrafts[addCredFamilyId] || '';
    applyAddFamily();
  });
  document.getElementById('add-cancel').addEventListener('click', closeModal);
  document.getElementById('add-ok').addEventListener('click', async () => {
    const status = document.getElementById('add-status');
    const ip = document.getElementById('add-ip').value.trim();
    if (!ip){ status.textContent = '请填写 IP 地址'; return; }
    const family = document.getElementById('add-family').value;
    const policy = credentialPolicy(family);
    const credential = document.getElementById('add-cred').value.trim();
    // 族要求必填时先在页面上拦住：发出去必然被服务端拒绝，
    // 用户只会看到一句「添加失败」，还得自己猜是哪里没填
    if (policy.required && !credential){
      status.textContent = '请填写' + policy.label + '（该设备族要求必填）';
      return;
    }
    // 端口与摄像头 URL 只对第三方族有意义：拓竹族一律传 0 / 空，
    // 免得隐藏起来的输入框里残留着上次的值、被服务端当成用户意图
    const thirdParty = isThirdPartyFamily(family);
    const port = parseInt(document.getElementById('add-port').value, 10);
    status.textContent = '正在添加…';
    const result = await postJson('/api/add_printer', {
      name: document.getElementById('add-name').value.trim(),
      ip: ip,
      serial: document.getElementById('add-serial').value.trim(),
      model: document.getElementById('add-model').value.trim(),
      // 设备族：空串在服务端即「拓竹」（老配置没有 family 字段时同理）
      family: family,
      // 0 / 非法值 = 用该族默认端口（服务端 _as_port 会把它们归零）
      port: thirdParty && port > 0 ? port : 0,
      camera_url: thirdParty ? document.getElementById('add-camera').value.trim() : '',
      // 凭据放进**该族对应的字段**：拓竹 access_code、第三方族 api_key。
      // 两个键都写出来（另一个给空串）是为了让服务端契约在这里一眼可见；
      // 空串服务端按「没填」处理，不会覆盖已有值。
      access_code: policy.key === 'access_code' ? credential : '',
      api_key: policy.key === 'api_key' ? credential : ''
    });
    if (result.ok){
      toast((result.detail || '已添加') + warnSuffix(result));
      closeModal();
      pollStatus();
    } else {
      status.textContent = '添加失败：' + (result.detail || '未知原因');
    }
  });
}

/* ---------------------------------------------------------------- 设备管理 */
async function openManage(){
  modalCard.innerHTML =
    '<h2>管理打印机</h2>' +
    '<div class="hint">可修改名称、凭据与端口，也可以重连、删除。删除会同时从配置里移除。</div>' +
    '<div id="mng-list" class="list"><div class="empty">加载中…</div></div>' +
    '<div class="actions"><button id="mng-close">关闭</button></div>';
  modal.style.display = 'flex';
  document.getElementById('mng-close').addEventListener('click', closeModal);
  await refreshManageList();
}

async function refreshManageList(){
  const list = document.getElementById('mng-list');
  let data;
  try{
    const response = await fetch(api('/api/printers'), {cache:'no-store'});
    if (response.status === 401){ showLogin(true); return; }
    data = await response.json();
  }catch(err){
    list.innerHTML = '<div class="empty">读取失败：' + escapeHtml(err) + '</div>';
    return;
  }
  const printers = data.printers || [];
  // 这份响应里也带着设备族表：存下来，接着打开「编辑」时标签才是对的
  cacheFamilies(data);
  if (!printers.length){
    list.innerHTML = '<div class="empty">还没有打印机，用「🔍 自动搜索」或「＋ 添加」加一台。</div>';
    return;
  }
  list.innerHTML = '';
  printers.forEach(item => {
    const row = document.createElement('div');
    row.className = 'item';
    row.innerHTML =
      '<span class="name">' + escapeHtml(item.name) +
      '<div class="meta">' + escapeHtml(item.ip) + ' · ' + escapeHtml(item.model) +
      (item.can_control ? '' : ' · 未连接') + '</div></span>';
    const edit = document.createElement('button');
    edit.textContent = '编辑';
    edit.addEventListener('click', () => openEdit(item));
    const reconnect = document.createElement('button');
    reconnect.textContent = '重连';
    reconnect.addEventListener('click', async () => {
      reconnect.disabled = true;
      const result = await postJson('/api/printers', {index: item.index, action:'reconnect'});
      toast(result.ok ? '正在重连…' : ('重连失败：' + (result.detail || '')));
      reconnect.disabled = false;
    });
    const remove = document.createElement('button');
    remove.textContent = '删除';
    remove.className = 'danger';
    remove.addEventListener('click', async () => {
      if (!confirm('确定删除 ' + item.name + '？\n（会从配置里移除）')) return;
      const result = await postJson('/api/printers', {index: item.index, action:'remove'});
      toast(result.ok ? '已删除' : ('删除失败：' + (result.detail || '')));
      await refreshManageList();
      pollStatus();
    });
    row.appendChild(reconnect);
    row.appendChild(edit);
    row.appendChild(remove);
    list.appendChild(row);
  });
}

async function openEdit(item){
  // 凭据标签要按族来写，但直接进编辑页时族表可能还没读过
  if (!(state.families || []).length) await ensureFamilies();
  // 设备族**不在这里改**：服务端 update 不接受 family（换族必须走「添加」那条路，
  // 会话实现会跟着换），所以这里不给族下拉，免得看起来像能把拓竹改成 Moonraker；
  // 只按该设备**实际的族**（逐台载荷里的 `family`）渲染凭据标签与端口提示。
  const family = itemFamilyId(item);
  const policy = credentialPolicy(family);
  // 端口：服务端逐台回的 `port` 已经是「设备实际用的端口，没有就用族默认端口」，
  // 直接拿它当占位提示，用户改之前就知道现在连的是哪个端口
  const currentPort = parseInt(item.port, 10) || (familyOf(family) || {}).default_port || 0;
  const candidates = (familyOf(family) || {}).candidate_ports || [];
  modalCard.innerHTML =
    '<h2>编辑 ' + escapeHtml(item.name) + '</h2>' +
    '<div class="hint">IP、机型与设备族由设备本身决定，不能在这里改。修改后需要重连生效。</div>' +
    '<label>名称</label><input id="ed-name" value="' + escapeHtml(item.name) + '">' +
    '<label id="ed-cred-label">凭据（留空表示不改）</label>' +
    '<input id="ed-cred" placeholder="' + (item.has_code ? '已保存 ' + item.code_len + ' 位' : '未填写') +
    '" autocomplete="off">' +
    '<label>端口（留空表示不改）</label>' +
    '<input id="ed-port" type="number" min="0" max="65535" inputmode="numeric" placeholder="' +
    (currentPort ? String(currentPort) : '0') + '">' +
    '<div class="hint" id="ed-port-hint"></div>' +
    '<div class="status" id="ed-status"></div>' +
    '<div class="actions"><button id="ed-back">返回</button>' +
    '<button id="ed-ok" class="primary">保存</button></div>';
  // 标签来自服务端数据（逐台的 credential_label，兜底用 families 表），前端不写死
  document.getElementById('ed-cred-label').textContent =
    itemCredentialLabel(item, family) + '（留空表示不改）';
  document.getElementById('ed-port-hint').textContent =
    '留空或 0 = 不改端口' + (currentPort ? ('，当前 ' + currentPort) : '') +
    (candidates.length ? ('；该族可选 ' + candidates.join(' / ')) : '');
  document.getElementById('ed-back').addEventListener('click', openManage);
  document.getElementById('ed-ok').addEventListener('click', async () => {
    const status = document.getElementById('ed-status');
    const credential = document.getElementById('ed-cred').value.trim();
    // 0 / 留空 = 不改端口（服务端只在 port > 0 时写入）
    const port = parseInt(document.getElementById('ed-port').value, 10) || 0;
    status.textContent = '正在保存…';
    const result = await postJson('/api/printers', {
      index: item.index, action: 'update',
      name: document.getElementById('ed-name').value.trim(),
      // 凭据只放进**该设备所属族对应的那一个字段**（拓竹 access_code / 第三方族 api_key）：
      // 服务端 manage_printer 是按设备实际的族挑字段的（另一个键原样忽略），
      // 所以另一个字段传空串是安全的 —— 也免得同一次请求里两个字段都被写上值。
      access_code: policy.key === 'access_code' ? credential : '',
      api_key: policy.key === 'api_key' ? credential : '',
      port: port > 0 ? port : 0
    });
    if (result.ok){
      toast((result.detail || '已保存') + warnSuffix(result));
      await openManage();
    } else {
      status.textContent = '保存失败：' + (result.detail || '未知原因');
    }
  });
}

/* ---------------------------------------------------------------- 设置 */
async function openSettings(){
  modalCard.innerHTML =
    '<h2>设置</h2><div class="hint">加载中…</div>' +
    '<div class="actions"><button id="set-close">关闭</button></div>';
  modal.style.display = 'flex';
  document.getElementById('set-close').addEventListener('click', closeModal);

  let data = {};
  try{
    const response = await fetch(api('/api/settings'), {cache:'no-store'});
    if (response.status === 501){
      modalCard.innerHTML = '<h2>设置</h2><div class="hint">' +
        '该运行方式不支持在网页上修改设置（请用桌面版的「⚙ 设置」）。</div>' +
        '<div class="actions"><button id="set-close2">关闭</button></div>';
      document.getElementById('set-close2').addEventListener('click', closeModal);
      return;
    }
    if (response.status === 401){ showLogin(true); return; }
    data = await response.json();
  }catch(err){
    modalCard.innerHTML = '<h2>设置</h2><div class="hint">读取失败：' +
      escapeHtml(err) + '</div><div class="actions"><button id="set-close3">关闭</button></div>';
    document.getElementById('set-close3').addEventListener('click', closeModal);
    return;
  }

  const fpsOptions = [[0,'不限制'],[4,'省电 4fps'],[8,'标准 8fps'],[10,'流畅 10fps'],[15,'很流畅 15fps']];
  const select = (id, value, options) => {
    let html = '<select id="' + id + '">';
    options.forEach(([v, label]) => {
      html += '<option value="' + v + '"' + (Number(value) === Number(v) ? ' selected' : '') +
              '>' + label + '</option>';
    });
    return html + '</select>';
  };

  modalCard.innerHTML =
    '<h2>设置</h2>' +
    '<div class="hint">改完点保存即生效。每路帧率对 6000 端口通道影响有限' +
    '（P1/A1 本身只有约 1fps）。</div>' +
    '<label>每路画面最大帧率</label>' +
    select('set-maxfps', data.max_fps, fpsOptions) +
    '<label>界面刷新间隔（毫秒）</label>' +
    '<input id="set-refresh" type="number" min="50" max="1000" step="50" value="' +
      Number(data.refresh_ms || 150) + '">' +
    '<label>网页帧率</label>' +
    '<input id="set-webfps" type="number" min="0.5" max="15" step="0.5" value="' +
      Number(data.web_fps || 4) + '">' +
    '<label>网页画面最大宽度（像素）</label>' +
    '<input id="set-webwidth" type="number" min="240" max="1920" step="60" value="' +
      Number(data.web_max_width || 720) + '">' +
    '<div class="status" id="set-status"></div>' +
    '<div class="actions"><button id="set-cancel">关闭</button>' +
    '<button id="set-ok" class="primary">保存</button></div>' +
    '<div id="set-conn"></div>' +
    '<div id="set-backup"></div>';
  document.getElementById('set-cancel').addEventListener('click', closeModal);
  document.getElementById('set-ok').addEventListener('click', async () => {
    const status = document.getElementById('set-status');
    status.textContent = '正在保存…';
    const result = await postJson('/api/settings', {
      max_fps: Number(document.getElementById('set-maxfps').value),
      refresh_ms: Number(document.getElementById('set-refresh').value),
      web_fps: Number(document.getElementById('set-webfps').value),
      web_max_width: Number(document.getElementById('set-webwidth').value)
    });
    if (result.ok){
      toast(result.detail || '设置已保存');
      closeModal();
    } else {
      status.textContent = '保存失败：' + (result.detail || '未知原因');
    }
  });

  // ---- 连接信息：令牌 + 局域网地址 ----
  // 安卓版没有终端，启动时那行「带令牌的地址」看不到；而令牌在页面加载后
  // 就被 history.replaceState 从 URL 上抹掉了（免得截图/分享时泄露）。
  // 所以必须显式给出来，否则用户只能在平板上用、没法在电脑上打开。
  try{
    const response = await fetch(api('/api/info'), {cache:'no-store'});
    if (!response.ok){
      document.getElementById('set-conn').innerHTML =
        '<label>在其它设备上打开</label><div class="hint">' +
        '该运行方式不提供连接信息（HTTP ' + response.status + '）。</div>';
      return;
    }
    const info = await response.json();
    const lan = info.lan_urls || [];
    const token = info.token || '';
    let html = '<label>在其它设备上打开</label>';
    if (lan.length){
      html += '<div class="hint">同一 Wi-Fi 下的手机或电脑，用下面任一条地址打开。' +
              '地址里已经带了访问令牌，直接复制即可，不用另外输入。</div>';
      lan.forEach((url, index) => {
        html += '<div class="conn-row"><code class="conn-url">' + escapeHtml(url) + '</code>' +
                '<button class="conn-copy" data-url="' + escapeHtml(url) + '">复制</button></div>';
      });
    } else {
      html += '<div class="hint">没有找到局域网地址 —— 这台设备可能没连 Wi-Fi，' +
              '或者只连了代理/虚拟网卡。连上和打印机同一个 Wi-Fi 后重开本页即可。</div>';
    }
    if (token){
      html += '<div class="conn-row"><span class="conn-label">访问令牌</span>' +
              '<code class="conn-token" id="conn-token">' + escapeHtml(token) + '</code>' +
              '<button class="conn-copy" data-token="' + escapeHtml(token) + '">复制</button></div>';
    }
    html += '<div class="hint">访问令牌是进入本页的口令，别发给不信任的人。' +
            '换设备打开时如果提示未授权，多半是令牌抄错了 —— 用上面的复制按钮。</div>';
    const box = document.getElementById('set-conn');
    box.innerHTML = html;
    box.querySelectorAll('.conn-copy').forEach(button => {
      button.addEventListener('click', async () => {
        const text = button.dataset.token || button.dataset.url || '';
        const ok = await copyText(text);
        toast(ok ? '已复制' : '复制失败，请长按手动选择');
      });
    });
  }catch(err){
    document.getElementById('set-conn').innerHTML =
      '<label>在其它设备上打开</label><div class="hint">读取连接信息失败：' +
      escapeHtml(String(err)) + '</div>';
  }

  // ---- 配置备份（导出 / 导入）----
  // 网页与安卓端原来**没有**任何配置备份入口（只有桌面版有菜单项），
  // 于是平板上既备份不了、也没法把电脑上的配置搬过来。
  renderBackupSection();
}

/* ------------------------------------------------------------ 配置备份 */

/**
 * 渲染「配置备份」区块。
 *
 * 关键设计：**口令是可选的，但差别很大** ——
 *   * 不带口令：访问代码按本机方式加密（Windows 是 DPAPI，绑定当前用户），
 *     只有同一台机器/同一用户能恢复；
 *   * 带口令：用标准库算法加密（`bmp1:`），**任何版本、任何平台**都能导入
 *     （Windows / Linux / Docker / 安卓）。
 * 安卓版没有 cryptography（APK 刻意不打包），所以两条路都必须能用 ——
 * 带口令那条恰恰是安卓唯一能导入的加密形式。
 */
function renderBackupSection(){
  const box = document.getElementById('set-backup');
  if (!box) return;
  box.innerHTML =
    '<label>配置备份（跨设备 / 跨版本）</label>' +
    '<div class="hint">导出会包含全部打印机与访问代码。' +
    '<b>建议设置口令</b>：带口令的配置文件可以在任何版本导入' +
    '（Windows 桌面版 / Linux / Docker / 安卓）。<br>' +
    '不设口令时访问代码按本机方式加密，只有同一台机器、同一用户才能恢复。</div>' +
    '<div class="conn-row">' +
      '<input id="backup-pass" type="password" placeholder="口令（建议填写）">' +
      '<button id="backup-export">导出</button>' +
      '<button id="backup-import">导入</button>' +
    '</div>' +
    '<div id="backup-status" class="status"></div>';
  document.getElementById('backup-export').addEventListener('click', doExportConfig);
  document.getElementById('backup-import').addEventListener('click', openImportConfig);
}

async function doExportConfig(){
  const status = document.getElementById('backup-status');
  const passphrase = document.getElementById('backup-pass').value;
  status.textContent = passphrase ? '正在生成（加密中）…' : '正在生成…';
  const result = await postJson('/api/config/export', {passphrase});
  if (!result.ok){ status.textContent = '导出失败：' + (result.detail || '未知原因'); return; }
  const text = result.json || '';
  status.textContent = '已生成 ' + (result.printers || 0) + ' 台设备的配置。' +
    (result.portable ? '（带口令，可在任何版本导入）' : '（未设口令：只有本机能恢复访问代码）');
  modalCard.innerHTML =
    '<h2>导出配置</h2>' +
    '<div class="hint">下面是完整的配置文件内容。三种拿走的方式，任选一种：<br>' +
    '① 点「下载文件」保存成 .json；② 点「复制」发给自己；' +
    '③ 在电脑上打开本页时也可以用 ①。<br>' +
    (result.portable ? '这份内容已用口令保护，导入时需要同一个口令。'
                     : '⚠ 未设口令：这份内容只有本机能恢复访问代码。') + '</div>' +
    '<textarea id="backup-text" readonly></textarea>' +
    '<div class="status" id="backup-status2"></div>' +
    '<div class="actions">' +
      '<button id="backup-download">下载文件</button>' +
      '<button id="backup-copy">复制</button>' +
      '<button id="backup-back">返回</button>' +
    '</div>';
  document.getElementById('backup-text').value = text;
  document.getElementById('backup-back').addEventListener('click', openSettings);
  document.getElementById('backup-copy').addEventListener('click', async () => {
    const ok = await copyText(text);
    document.getElementById('backup-status2').textContent = ok ? '已复制到剪贴板' : '复制失败，请长按手动选择';
  });
  document.getElementById('backup-download').addEventListener('click', () => {
    try{
      const blob = new Blob([text], {type:'application/json'});
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'bambu-monitor-config.json';
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      setTimeout(() => URL.revokeObjectURL(url), 5000);
      document.getElementById('backup-status2').textContent =
        '已开始下载（若没有反应，请用「复制」把内容带走）';
    }catch(err){
      document.getElementById('backup-status2').textContent =
        '无法直接下载（本应用的浏览器内核可能不支持），请用「复制」：' + err;
    }
  });
}

function openImportConfig(){
  modalCard.innerHTML =
    '<h2>导入配置</h2>' +
    '<div class="hint">把导出的配置文件内容粘贴到下面，然后点「导入」。<br>' +
    '如果那份文件是用口令加密的，请填上同一个口令；' +
    '带口令的文件在任何版本都能导入（Windows / Linux / Docker / 安卓）。<br>' +
    '⚠ 导入会用文件里的设备**替换**当前配置，并重新连接。</div>' +
    '<input id="import-file" type="file" accept=".json,application/json" style="display:none">' +
    '<button id="import-pick">从文件读取…</button>' +
    '<textarea id="import-text" placeholder="在此粘贴配置内容…"></textarea>' +
    '<input id="import-pass" type="password" placeholder="口令（文件是加密的就填）">' +
    '<div class="status" id="import-status"></div>' +
    '<div class="actions">' +
      '<button id="import-cancel">返回</button>' +
      '<button id="import-ok" class="primary">导入</button>' +
    '</div>';
  document.getElementById('import-cancel').addEventListener('click', openSettings);
  document.getElementById('import-pick').addEventListener('click',
    () => document.getElementById('import-file').click());
  document.getElementById('import-file').addEventListener('change', async (event) => {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    try{
      document.getElementById('import-text').value = await file.text();
      document.getElementById('import-status').textContent = '已读取 ' + file.name;
    }catch(err){
      document.getElementById('import-status').textContent = '读取文件失败：' + err;
    }
  });
  document.getElementById('import-ok').addEventListener('click', async () => {
    const status = document.getElementById('import-status');
    const text = document.getElementById('import-text').value.trim();
    const passphrase = document.getElementById('import-pass').value;
    if (!text){ status.textContent = '请先粘贴配置内容，或用「从文件读取」。'; return; }
    status.textContent = '正在导入并重连…';
    const result = await postJson('/api/config/import', {json: text, passphrase});
    if (!result.ok){ status.textContent = '导入失败：' + (result.detail || '未知原因'); return; }
    toast(result.detail || '已导入');
    if (result.warning) toast(result.warning);
    // 配置换了，整页重载最干脆（会话已重建，索引也对得上）
    setTimeout(() => location.reload(), 800);
  });
}

/** 复制文本：优先用异步剪贴板 API（需要安全上下文），失败退回 execCommand。
 *  局域网 HTTP 访问时 navigator.clipboard 常常不可用，所以兜底是必须的。 */
async function copyText(text){
  try{
    if (navigator.clipboard && window.isSecureContext){
      await navigator.clipboard.writeText(text);
      return true;
    }
  }catch(err){ /* 落到下面的兜底 */ }
  try{
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.left = '-9999px';
    document.body.appendChild(area);
    area.select();
    area.setSelectionRange(0, area.value.length);   // iOS 需要
    const ok = document.execCommand('copy');
    document.body.removeChild(area);
    return ok;
  }catch(err){
    return false;
  }
}

/* ---------------------------------------------------------------- 单画面菜单 */
function tileMenu(index, item){
  menu.innerHTML = '';
  const add = (label, handler, className) => {
    const button = document.createElement('button');
    button.textContent = label;
    if (className) button.className = className;
    button.addEventListener('click', () => { closeMenu(); handler(); });
    menu.appendChild(button);
  };
  const sep = () => {
    const line = document.createElement('div');
    line.className = 'sep';
    menu.appendChild(line);
  };

  add('单画面 / 还原', () => {
    const tile = state.tiles.get(index);
    if (tile) tile.element.classList.toggle('full');
  });
  add('抓拍保存图片', () => snapshotTile(index, item));
  // 通道诊断与画面大小：桌面端有，网页/安卓端以前没有 ——
  // 平板上遇到「画面出不来」只能干瞪眼，也没法把关键那台放大
  add('通道诊断…', () => openDiagnose(index, item));
  add(item.span > 1 ? '取消重点画面' : '设为重点画面（2×2）',
      () => toggleSpan(index, item));
  add('复制 IP', async () => {
    try{
      await navigator.clipboard.writeText(item.ip);
      toast('已复制 ' + item.ip);
    }catch(err){ toast('复制失败（浏览器未授权剪贴板）'); }
  });
  sep();
  add('重连', async () => {
    const result = await postJson('/api/printers', {index: index, action:'reconnect'});
    toast(result.ok ? '正在重连…' : ('重连失败：' + (result.detail || '')));
  });
  add('编辑名称 / 凭据', () => {
    openManage().then(() => openEdit(Object.assign({index: index}, item)));
  });
  add('删除这台设备', async () => {
    if (!confirm('确定删除 ' + item.name + '？')) return;
    const result = await postJson('/api/printers', {index: index, action:'remove'});
    toast(result.ok ? '已删除' : ('删除失败：' + (result.detail || '')));
    pollStatus();
  });
}

/* ---------------------------------------------------------------- 通道诊断 / 画面大小 */

/**
 * 跑一遍通道诊断并把报告显示出来。
 *
 * 服务端复用 `app/bambu/diagnostics.py`（与桌面端对话框、命令行工具同一份实现），
 * 所以三处结论一致。一轮约 10~20 秒，期间显示「诊断中」。
 */
async function openDiagnose(index, item){
  modalCard.innerHTML =
    '<h2>通道诊断 · ' + escapeHtml(item.name || item.ip) + '</h2>' +
    '<div class="hint">逐项检查端口 / TLS / 6000 画面 / RTSPS 鉴权 / MQTT 遥测。' +
    '约 10-20 秒，期间请保持页面打开。</div>' +
    '<div class="status" id="diag-status">正在诊断…</div>' +
    '<div id="diag-report"></div>' +
    '<div class="actions"><button id="diag-close">关闭</button></div>';
  modal.style.display = 'flex';
  document.getElementById('diag-close').addEventListener('click', closeModal);

  let data;
  try{
    const response = await fetch(api('/api/diagnose?index=' + index), {cache:'no-store'});
    if (response.status === 501){
      document.getElementById('diag-status').textContent =
        '该运行方式不支持在网页上做诊断（请用桌面版的「画面通道诊断」，或电脑上的 tools/diagnose.py）。';
      return;
    }
    if (response.status === 401){ showLogin(true); return; }
    data = await response.json();
  }catch(err){
    document.getElementById('diag-status').textContent = '诊断请求失败：' + err;
    return;
  }
  if (!data.ok){
    document.getElementById('diag-status').textContent = '诊断失败：' + (data.detail || '未知原因');
    return;
  }
  let lines = [];
  (data.sections || []).forEach(section => {
    if (section.title) lines.push(section.title);
    (section.lines || []).forEach(line => lines.push(line));
    lines.push('');
  });
  (data.tail || []).forEach(line => lines.push(line));
  const report = lines.join('\n');
  document.getElementById('diag-status').textContent =
    '诊断完成（' + escapeHtml(data.name || '') + ' ' + escapeHtml(data.ip || '') + '）';
  document.getElementById('diag-report').innerHTML =
    '<textarea id="diag-text" readonly></textarea>' +
    '<div class="actions"><button id="diag-copy">复制报告</button></div>';
  document.getElementById('diag-text').value = report;
  document.getElementById('diag-copy').addEventListener('click', async () => {
    const ok = await copyText(report);
    toast(ok ? '报告已复制' : '复制失败，请长按手动选择');
  });
}

/** 切换「重点画面」（1 格 <-> 2×2）。 */
async function toggleSpan(index, item){
  const target = (item.span || 1) > 1 ? 1 : 2;
  const result = await postJson('/api/layout', {index: index, span: target});
  toast(result.ok ? (result.detail || '已更新') : ('调整失败：' + (result.detail || '')));
  if (result.ok) pollStatus();
}

/** 抓拍：把当前帧下成图片。 */
async function snapshotTile(index, item){
  try{
    const response = await fetch(api('/api/frame/' + index), {cache:'no-store'});
    if (!response.ok){ toast('当前没有画面'); return; }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    link.href = url;
    link.download = (item.name || ('printer-' + index)) + '-' + stamp + '.jpg';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    toast('已保存抓拍');
  }catch(err){ toast('抓拍失败：' + err); }
}

/** 给画面绑定右键菜单（长按在触屏上等价）。 */
function bindTileMenu(element, index){
  const open = (event) => {
    const item = (state.lastPrinters || [])[index];
    if (!item) return;
    event.preventDefault();
    const rect = menu.getBoundingClientRect();
    menu.style.display = 'block';
    let x = event.clientX, y = event.clientY;
    if (x + rect.width > window.innerWidth) x = window.innerWidth - rect.width - 8;
    if (y + rect.height > window.innerHeight) y = window.innerHeight - rect.height - 8;
    menu.style.left = Math.max(4, x) + 'px';
    menu.style.top = Math.max(4, y) + 'px';
    tileMenu(index, item);
  };
  element.addEventListener('contextmenu', open);
  // 触屏长按（500ms）等价于右键。
  // ⚠️ 弹完菜单要打一个标记：否则长按抬手时会紧接着触发 click，
  // 顺带把画面切成全屏 —— 用户看到的是"长按弹出菜单，同时画面放大了"。
  let timer = null;
  let moved = false;
  element.addEventListener('touchstart', (event) => {
    const touch = event.touches[0];
    moved = false;
    timer = setTimeout(() => {
      element.dataset.menuJustOpened = '1';
      open({preventDefault(){}, clientX: touch.clientX, clientY: touch.clientY});
    }, 500);
  }, {passive: true});
  element.addEventListener('touchmove', () => {
    moved = true;
    if (timer){ clearTimeout(timer); timer = null; }
  }, {passive: true});
  ['touchend','touchcancel'].forEach(type =>
    element.addEventListener(type, () => {
      if (timer){ clearTimeout(timer); timer = null; }
      if (moved){ delete element.dataset.menuJustOpened; }
    }, {passive: true}));
}

/* ---------------------------------------------------------------- 入口绑定 */
document.getElementById('btn-discover').addEventListener('click', openDiscover);
document.getElementById('btn-add').addEventListener('click', openAdd);
document.getElementById('btn-manage').addEventListener('click', openManage);
document.getElementById('btn-settings').addEventListener('click', openSettings);
</script>
</body>
</html>
"""
