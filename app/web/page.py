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
  <button id="btn-reload">刷新</button>
  <div id="clock"></div>
</header>
<main id="wall"></main>

<div id="hmsbox"></div>
<div id="toast"></div>

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
  element.addEventListener('click', () => element.classList.toggle('full'));
  document.addEventListener('keydown', e => { if (e.key === 'Escape') element.classList.remove('full'); });
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
    lightButton.disabled = !info.can_control;

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
</script>
</body>
</html>
"""
