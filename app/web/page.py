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
  #modal .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  #modal .row > *{flex:1 1 140px}
  #modal button{background:#1d272e;border:1px solid var(--border);border-radius:6px;
        color:var(--fg);padding:8px 14px;font-size:13px;cursor:pointer;font-family:inherit}
  #modal button:hover{border-color:var(--accent);color:var(--accent)}
  #modal button.primary{background:var(--accent);color:#06222a;border-color:var(--accent);font-weight:bold}
  #modal button.danger:hover{border-color:var(--err);color:var(--err)}
  #modal .actions{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
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
const state = {tiles: new Map(), token: "", live: false, liveFails: 0, fallback: false};
const wall = document.getElementById('wall');
const MAGIC_A = 0x42, MAGIC_B = 0x4D, HEADER = 9, KIND_FRAME = 1, KIND_STATUS = 2;

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

function applyStatus(data){
  const printers = data.printers || [];
  // 记下来给右键菜单用（菜单要知道当前是哪台设备）
  state.lastPrinters = printers;
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
    element.querySelector('.status-text').textContent = info.status_text;

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
    // 控制被固件签名要求挡住时（新机型未开开发者模式），把原因挂到提示上：
    // 按钮是灰的却不说明原因，用户会以为软件坏了
    const blocked = info.controls_blocked_reason || '';
    [pauseButton, stopButton, lightButton].forEach(button => {
      button.title = blocked;
    });
    // 触屏看不到 tooltip，所以在状态条里显示一条可点击的提示条。
    // 这条提示是"用户能自己解决问题"的关键：告诉他去打印机上开开发者模式。
    //
    // ⚠️ 文案必须**逐条命令**说清楚，不能笼统写"控制不可用"。
    // 固件签名要求只覆盖 `print` 段的命令（暂停/停止/速度），
    // 而灯控走 `system` 段、**不受影响**。曾经这里统一显示
    // 「⚠ 控制不可用」，结果在安卓上用户看到"控制不可用"却发现灯明明能开关
    // —— 提示与事实矛盾，比不提示更糟。
    const hint = element.querySelector('.ctrl-hint');
    if (blocked){
      // 与后端 SIGNATURE_SENSITIVE_COMMANDS 一致：哪些命令真的被挡
      const blockedActs = (info.printing ? ['pause', 'stop'] : [])
        .filter(act => {
          const button = element.querySelector('button[data-act="' + act + '"]');
          return button && button.style.display !== 'none';
        });
      const hurt = blockedActs.length > 0;
      if (hurt){
        hint.textContent = '⚠ ' + blockedActs.length + ' 项控制被固件挡住（点此查看原因）';
      } else {
        // 当前没有被挡住的可见控制（例如空闲时没有暂停/停止按钮），
        // 此时不该报"控制不可用" —— 灯控等仍然可用
        hint.textContent = 'ℹ 部分命令（暂停/停止/速度）被固件挡住；开关灯不受影响（点此查看原因）';
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
    } else if (kind === KIND_STATUS){
      try { applyStatus(JSON.parse(new TextDecoder().decode(payload))); } catch (err) { /* 忽略坏包 */ }
    }
  }
  let rest = buffer.slice(offset);
  if (rest.length > 16 * 1024 * 1024) rest = new Uint8Array(0);  // 防御：异常数据不要无限增长
  return rest;
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

/* ---------------------------------------------------------------- 手动添加 */
function openAdd(){
  modalCard.innerHTML =
    '<h2>添加打印机</h2>' +
    '<div class="hint">填 IP 与访问代码即可显示画面。<b>序列号建议一并填写</b>：' +
    '进度、温度这些遥测数据靠它订阅，缺了会出现「有画面但没有进度/温度」。' +
    '在打印机屏幕上：设置 → 设备信息。</div>' +
    '<label>名称（可留空）</label><input id="add-name" placeholder="例如 车间 X2D">' +
    '<label>IP 地址</label><input id="add-ip" placeholder="192.168.1.50" inputmode="decimal">' +
    '<label>访问代码</label><input id="add-code" placeholder="8 位访问代码" autocomplete="off">' +
    '<label>序列号（建议填写，用于遥测）</label>' +
    '<input id="add-serial" placeholder="例如 01P00A123456789" autocomplete="off">' +
    '<label>机型（可留空，自动识别）</label><input id="add-model" placeholder="例如 X2D">' +
    '<div class="status" id="add-status"></div>' +
    '<div class="actions"><button id="add-cancel">取消</button>' +
    '<button id="add-ok" class="primary">添加</button></div>';
  modal.style.display = 'flex';
  document.getElementById('add-cancel').addEventListener('click', closeModal);
  document.getElementById('add-ok').addEventListener('click', async () => {
    const status = document.getElementById('add-status');
    const ip = document.getElementById('add-ip').value.trim();
    if (!ip){ status.textContent = '请填写 IP 地址'; return; }
    status.textContent = '正在添加…';
    const result = await postJson('/api/add_printer', {
      name: document.getElementById('add-name').value.trim(),
      ip: ip,
      access_code: document.getElementById('add-code').value.trim(),
      serial: document.getElementById('add-serial').value.trim(),
      model: document.getElementById('add-model').value.trim()
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
    '<div class="hint">可修改名称与访问代码、重连、删除。删除会同时从配置里移除。</div>' +
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

function openEdit(item){
  modalCard.innerHTML =
    '<h2>编辑 ' + escapeHtml(item.name) + '</h2>' +
    '<div class="hint">IP 与机型由设备决定，不能在这里改。修改后需要重连生效。</div>' +
    '<label>名称</label><input id="ed-name" value="' + escapeHtml(item.name) + '">' +
    '<label>访问代码（留空表示不改）</label>' +
    '<input id="ed-code" placeholder="' + (item.has_code ? '已保存 ' + item.code_len + ' 位' : '未填写') +
    '" autocomplete="off">' +
    '<div class="status" id="ed-status"></div>' +
    '<div class="actions"><button id="ed-back">返回</button>' +
    '<button id="ed-ok" class="primary">保存</button></div>';
  document.getElementById('ed-back').addEventListener('click', openManage);
  document.getElementById('ed-ok').addEventListener('click', async () => {
    const status = document.getElementById('ed-status');
    status.textContent = '正在保存…';
    const result = await postJson('/api/printers', {
      index: item.index, action: 'update',
      name: document.getElementById('ed-name').value.trim(),
      access_code: document.getElementById('ed-code').value.trim()
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
    '<button id="set-ok" class="primary">保存</button></div>';
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
  add('编辑名称 / 访问代码', () => {
    openManage().then(() => openEdit(Object.assign({index: index}, item)));
  });
  add('删除这台设备', async () => {
    if (!confirm('确定删除 ' + item.name + '？')) return;
    const result = await postJson('/api/printers', {index: index, action:'remove'});
    toast(result.ok ? '已删除' : ('删除失败：' + (result.detail || '')));
    pollStatus();
  });
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
