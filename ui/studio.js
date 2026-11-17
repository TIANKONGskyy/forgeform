const $=id=>document.getElementById(id);let selected='',lastScan=0,lastAction=null,launchPending=false,models=[],scanData=null;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(s){$('toast').textContent=s;$('toast').hidden=false;clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>$('toast').hidden=true,6500)}
async function api(path,data){const r=await fetch('/api/'+path,{method:data===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-Studio-Token':window.STUDIO_TOKEN},body:data===undefined?undefined:JSON.stringify(data)});const j=await r.json();if(!r.ok)throw Error(j.error||'请求失败');return j}
function page(id){document.querySelectorAll('.page').forEach(e=>e.hidden=e.id!==id);document.querySelectorAll('nav button').forEach(e=>e.classList.toggle('active',e.dataset.page===id));$('breadcrumb').textContent=({overview:'工作台',setup:'安装与环境',models:'模型与配置',create:'创作空间'})[id];if(id==='models'){catalog();refreshDelModel();if(typeof refreshKeys==='function')refreshKeys();}if(id==='overview'){hwStart();}else{hwStop();}}
document.querySelectorAll('[data-page]').forEach(b=>b.addEventListener('click',()=>page(b.dataset.page)));
// 深链接挪到文件末尾再执行。
// ★ 2026-11-16 事故：这段原来放在这里（第 8 行），而 `page()` 会调 hwStop()，
//   `HW` 是后面才 `const` 声明的 —— 带 #create / #setup 打开时直接抛
//   "Cannot access 'HW' before initialization"，**后面所有按钮的 onclick 都没绑上**，
//   用户看到的就是"点检测环境、点打开创作空间都没反应"。
$('theme').onclick=()=>{document.body.classList.toggle('light');const theme=document.body.classList.contains('light')?'light':'dark';localStorage.setItem('forgeform-theme',theme);const frame=$('workspace');if(frame&&frame.contentWindow&&frame.getAttribute('src')){try{frame.contentWindow.postMessage({type:'forgeform-theme',theme},new URL(frame.src).origin)}catch(_){}}};if(localStorage.getItem('forgeform-theme')==='light')document.body.classList.add('light');
// 2026-11-16：用户反馈"点了没反应"。原来的 action() 只在 catch 里 toast，
// 成功路径**什么反馈都没有** —— 而检测环境/启动创作空间都可能几秒到几十秒
// （或者本来就已经就绪，瞬间完成），界面上看起来就像没点。现在：
//   · 立刻把按钮置灰 + 顶栏写"正在…" + toast 一句，让点击必有回显
//   · 出错时把错误同时写进顶栏和 toast（不再只有一个转瞬即逝的提示）
const ACTION_LABEL={scan:'检测环境',runtime:'安装基础环境',install:'安装所选方案',
  launch:'打开创作空间',cancel:'取消任务',keys:'密钥操作',calibrate:'校准本机耗时',
  location:'安装位置',firstrun:'一键开始',cloud:'云端配置'};
async function action(name,data={},btn=null){
  const label=ACTION_LABEL[name]||name;
  const old=btn?btn.textContent:'';
  if(btn){btn.disabled=true;btn.textContent='处理中…'}
  toast('已提交：'+label);
  try{$('top-status').textContent=label+'中…'}catch(e){}
  try{
    if(name!=='cancel')lastAction={name,data};
    const r=await api(name,data);
    await poll();
    toast(label+'：已提交成功');
    return r;
  }catch(e){
    const msg=label+'失败：'+(e&&e.message||e);
    toast(msg);
    try{$('top-status').textContent='操作失败'}catch(_){}
    throw e;
  }finally{
    if(btn){btn.disabled=false;btn.textContent=old||'确定'}
  }
}
$('scan').onclick=(ev)=>action('scan',{model_root:$('model-root').value},ev.currentTarget);$('runtime').onclick=(ev)=>action('runtime',{},ev.currentTarget);$('install').onclick=(ev)=>{if(selected)action('install',{key:selected,llm:$('install-llm').value},ev.currentTarget)};$('cancel').onclick=()=>action('cancel');$('launch').onclick=(ev)=>{launchPending=true;action('launch',{},ev.currentTarget)};
function renderScan(s){if(!s||s.scanned_at===lastScan)return;lastScan=s.scanned_at;scanData=s;selected=s.default;$('install-llm').innerHTML='<option value="">按方案自动选择 / 复用已有服务</option>'+(s.llm_choices||[]).map(c=>`<option value="${esc(c.model)}" ${c.fits?'':'disabled'}>${esc(c.label)} · ${c.size_gb} GB${c.fits?'':' · 资源不足'}</option>`).join('');$('model-root').value=s.model_root;const h=s.hardware;$('hardware').innerHTML=[['显卡',h.gpu_name||'未检测到 NVIDIA',`${h.vram_gb} GB 显存 · 空闲 ${h.vram_free_gb} GB`],['系统内存',`${h.ram_gb} GB`,h.ram_available_gb==null?'加载峰值需额外余量':`当前可用 ${h.ram_available_gb} GB`],['可用磁盘',`${h.disk_free_gb} GB`,'按模型保存位置检测'],['已有环境',s.frameworks.filter(f=>f.alive).length+' 个','只读探测，不预热模型']].map(([a,b,c])=>`<article><small>${esc(a)}</small><strong>${esc(b)}</strong><p>${esc(c)}</p></article>`).join('');$('frameworks').innerHTML=s.frameworks.map(f=>`<span class="${f.alive?'found':''}" title="${esc(f.detail||f.note)}">${f.alive?'●':'○'} ${esc(f.label)}${f.alive?' · '+f.models.length+' 个模型':''}</span>`).join('')+(s.frameworks||[]).filter(f=>f.key==='diffusers'&&f.note).map(f=>`<p class="framework-warning">${esc(f.note)}</p>`).join('');$('scan-time').textContent='刚刚检测';$('plans').innerHTML=s.profiles.map(p=>`<button class="plan ${p.key===selected?'selected':''}" data-plan="${esc(p.key)}" ${!p.fits?'disabled':''}><span class="tag">${p.key===s.default?'推荐方案':p.fits?'可选方案':'当前不适合'}</span><h3>${esc(p.name)}</h3><p>${esc(p.desc)}</p><div class="size">${p.download_gb} <small>GB 下载</small></div><p>${esc(p.reason)}</p>${(p.quant_hints||[]).map(h=>`<div class="hint">${esc(h.model)} · ${esc(h.quant)}${h.offload?' / 内存卸载':''}<br>预计显存 ${h.vram_need_gb} GB · 内存 ${h.ram_need_gb} GB${h.fits?'':' · 资源需复核'}</div>`).join('')}</button>`).join('')||'<div class="empty">没有找到可用方案。可在模型与配置中接入已有服务。</div>';$('plans').querySelectorAll('[data-plan]').forEach(b=>b.onclick=()=>{selected=b.dataset.plan;$('plans').querySelectorAll('.plan').forEach(p=>p.classList.toggle('selected',p===b));$('install').disabled=false;installationEstimate()});$('install').disabled=!selected;installationEstimate();}
async function catalog(){try{const s=await api('catalog');models=s.models;for(const [id,kind] of [['image','image'],['mesh','3d']]){const e=$(id);const current=s.config.stages[kind]?.default_model||'';const options=models.filter(p=>p.kind===kind);e.innerHTML='<option value="">保持当前模型</option>'+options.map(p=>`<option value="${esc(p.id)}" ${p.unavailable_reason?'disabled':''}>${esc(p.label)}${p.unavailable_reason?' · 暂不可用':''}${p.installed?' · 本地已有':''}</option>`).join('');if(options.some(p=>p.id===current))e.value=current;e.onchange=()=>{$(id+'_path').value=options.find(p=>p.id===e.value)?.path||''};e.onchange();}$('mode').value=s.config.backend.mode||'auto';$('catalog').innerHTML=models.map(p=>`<div class="catalog-item"><b>${esc(p.label)}</b><p class="${p.installed?'installed':''}">${p.unavailable_reason?esc(p.unavailable_reason):p.installed?'本地已有':'待安装'} · ${esc(p.kind)}</p><p>${esc(p.path||p.repo)}</p></div>`).join('')}catch(e){toast(e.message)}}
$('settings').onsubmit=async e=>{e.preventDefault();try{const r=await api('config',{mode:$('mode').value,quant:$('quant').value,image:$('image').value,'3d':$('mesh').value,image_path:$('image_path').value,'3d_path':$('mesh_path').value,llm:$('llm').value,endpoint:$('endpoint').value});$('save-result').textContent=r.message;toast('配置已保存')}catch(e){$('save-result').textContent=e.message;toast(e.message)}};
// A live process is not yet a listening Gradio service. Probe its real
// config before assigning iframe.src; browsers fire iframe load even for an
// error page, so an early assignment otherwise leaves a permanently blank tab.
let workspaceProbing = false;
async function syncWorkspace(s) {
  const frame = $('workspace'), empty = $('create-empty'), wrap = $('ws-wrap');
  if (!frame || !empty || !wrap) return;
  if (!s.ready) {
    if (frame.getAttribute('src')) {
      frame.removeAttribute('src');
      delete frame.dataset.appBase;
      frame.hidden = true; wrap.hidden = true; empty.hidden = false;
    }
    return;
  }
  const base = String(s.app_url || '').replace(/\/+$/, '');
  if (!base || (frame.getAttribute('src') && frame.dataset.appBase === base) || workspaceProbing) return;
  workspaceProbing = true;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 2500);
  try {
    const response = await fetch(base + '/config', {cache: 'no-store', signal: controller.signal});
    if (!response.ok) return;
    const config = await response.json();
    if (!Array.isArray(config.components) || !Array.isArray(config.dependencies)) return;
    const theme = document.body.classList.contains('light') ? 'light' : 'dark';
    const url = base + '/?__theme=' + theme;
    frame.dataset.appBase = base;
    if (window.__wsWait) window.__wsWait(url);
    frame.src = url;
    frame.hidden = false; wrap.hidden = false; empty.hidden = true;
    launchPending = false;
  } catch (_) {
    // Keep the launch state visible; the next status poll will retry.
  } finally {
    clearTimeout(timer);
    workspaceProbing = false;
  }
}
async function poll(){try{const s=await api('status'),j=s.job;
  try{if(launchPending&&!s.ready){const el=document.querySelector('#create-empty p');if(el)el.textContent='创作空间正在启动（约 10-20 秒，首次加载更久）…'}}catch(_){}
  await syncWorkspace(s);renderScan(s.snapshot);$('top-status').textContent=j.status==='running'?j.label:s.runtime_ready?'环境已安装':'待准备环境';$('job').hidden=j.status==='idle';$('job-title').textContent=j.label;$('job-kind').textContent=({running:'正在进行',done:'完成',failed:'未完成 · 可重试',cancelled:'已取消'})[j.status]||'';$('logs').textContent=j.logs.join('\n');$('cancel').hidden=j.status!=='running';if(j.total){$('progress').max=j.total;$('progress').value=j.step;$('job-step').textContent=`已完成 ${j.step} / ${j.total} 个步骤`}else{$('progress').removeAttribute('value');$('job-step').textContent=j.status==='running'?'正在处理，详细记录会持续更新。':''}
// 下载中的文件级进度：只显示"第几步"时，30 GB 的模型看着像卡死
if(j.status==='running'&&j.file){$('download').hidden=false;$('download-name').textContent=j.file;$('download-pct').textContent=(j.percent||0).toFixed(1)+'%';{const eta=j.eta_sec||0;const etaTxt=eta>0?` · 剩余约 ${eta>=60?Math.floor(eta/60)+' 分 '+(eta%60)+' 秒':eta+' 秒'}`:'';
$('download-detail').textContent=`${(j.downloaded_mb||0).toFixed(0)} / ${(j.size_mb||0).toFixed(0)} MB · ${(j.speed_mbps||0).toFixed(2)} MB/s${etaTxt}`};$('download-bar').style.width=Math.min(100,j.percent||0)+'%'}else{$('download')&&($('download').hidden=true)}for(const id of ['scan','runtime','launch'])$(id).disabled=j.status==='running';$('install').disabled=j.status==='running'||!selected;if(j.status==='failed'||j.status==='cancelled'){launchPending=false;if(!$('retry')){const b=document.createElement('button');b.id='retry';b.textContent='重试此任务';b.onclick=()=>{if(lastAction)action(lastAction.name,lastAction.data)};$('job').append(b)}}else $('retry')?.remove()}catch(e){$('top-status').textContent='连接中断';if(!window.connectionError){toast('本地服务连接中断，请重新打开启动程序');window.connectionError=true}}}
poll();setInterval(poll,1500);

// ── 删除：单模型 / 整个铸想 ──
// 都走"先看清单再删"：purge-plan 显示要删什么、多大；删除前 confirm 一次。
async function refreshDelModel(){
  try{
    const s = await api('catalog');
    const sel = $('del-model');
    if(!sel) return;
    const installed = (s.models||[]).filter(m => m.installed);
    sel.innerHTML = installed.length
      ? installed.map(m => `<option value="${esc(m.id)}">${esc(m.label)}（${esc(m.kind)}）</option>`).join('')
      : '<option value="">没有已安装的铸想模型</option>';
  }catch(e){}
}

$('del-model-go')?.addEventListener('click', async () => {
  const id = $('del-model')?.value;
  if(!id){ $('del-model-msg').textContent = '没有可删的模型'; return; }
  if(!confirm(`删除模型「${id}」的本地权重？\n只删铸想目录下的文件，ComfyUI 的共享权重不动。\n删除后要重新下载才能用。`)) return;
  $('del-model-msg').textContent = '正在删除…';
  try{
    const r = await api('remove', {id});
    $('del-model-msg').textContent = r.ok ? `已删除，释放 ${r.freed_gb} GB` : (r.error || '删除失败');
    await refreshDelModel();
    await catalog();
  }catch(e){ $('del-model-msg').textContent = '删除失败：' + e.message; }
});

$('purge-inspect')?.addEventListener('click', async () => {
  try{
    const plan = await api('purge-plan');
    const box = $('purge-plan');
    $('purge-total').textContent = `共 ${plan.total_gb} GB`;
    box.innerHTML = plan.items.length
      ? plan.items.map(i => `<div class="purge-item"><b>${esc(i.label)}</b>
          <span>${i.size_gb} GB</span>
          <small>${esc(i.note || '')}</small>
          <code>${esc(i.path)}</code></div>`).join('')
      : '<p class="muted">没有可删的内容。</p>';
  }catch(e){ toast('读取失败：' + e.message); }
});

$('purge-all')?.addEventListener('click', async () => {
  const ok = confirm(
    '删除铸想在本机生成的全部数据？\n\n' +
    '包含：Python 环境(.venv)、铸想下的模型权重、生成结果、日志、对话组存档、配置文件。\n' +
    '不含：ComfyUI 及其权重、仓库源码本身。\n\n' +
    '删除后无法恢复，需要重新安装。确定继续？'
  );
  if(!ok) return;
  const twice = prompt('再确认一次：输入 删除 两个字');
  if(twice !== '删除'){ toast('已取消'); return; }
  await action('purge', {scope: 'all'});
});

function installationEstimate(){
  const plan=scanData?.profiles.find(p=>p.key===selected);
  const choice=scanData?.llm_choices.find(c=>c.model===$('install-llm').value);
  if(!plan){$('install-estimate').textContent='';return}
  let size=plan.download_gb;
  if(choice){
    size-=plan.downloads.filter(d=>d.kind==='llm').reduce((n,d)=>n+d.gb,0);
    size+=choice.size_gb;
    if(!plan.downloads.some(d=>d.plugin==='ollama'))size+=0.7;
  }
  $('install-estimate').textContent=`当前组合预计下载 ${size.toFixed(2)} GB（卡片为默认组合）`;
}
$('install-llm').onchange=installationEstimate;

// ── 硬件实时监控（overview 页）──────────────────────────────
// 每 2 秒拉 /api/hardware。monitor.py 那边维护滚动历史，这里只负责画。
const HW = { timer: 0, shown: false };
const HW_COL = { gpu: '#9ce4da', vram: '#8fb8f0', ram: '#c4a8f0', cpu: '#f0c48f' };

function spark(id, series, color, max) {
  const cv = $(id);
  if (!cv || !series || !series.length) return;
  const dpr = Math.min(devicePixelRatio || 1, 2);
  const w = cv.clientWidth || 300;
  const h = cv.clientHeight || 44;
  if (cv.width !== w * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  const n = series.length;
  const hi = max || Math.max(...series, 1);
  g.beginPath();
  series.forEach((v, i) => {
    const x = n === 1 ? w : (i / (n - 1)) * w;
    const y = h - 2 - (Math.min(v, hi) / hi) * (h - 6);
    i ? g.lineTo(x, y) : g.moveTo(x, y);
  });
  g.strokeStyle = color;
  g.lineWidth = 1.6;
  g.lineJoin = 'round';
  g.stroke();
  // 面积填充
  g.lineTo(w, h);
  g.lineTo(0, h);
  g.closePath();
  g.fillStyle = color + '18';
  g.fill();
}

function setText(id, v) { const el = $(id); if (el) el.textContent = v; }

async function hardware() {
  let s;
  try {
    s = await api('hardware');
  } catch (e) {
    // 别静默失败：面板得能看出是"取不到"还是"没数据"
    setText('hw-stamp', '采集失败：' + (e && e.message ? e.message : e));
    return;
  }
  if (!s || !s.ok) {
    setText('hw-stamp', (s && s.error) ? ('采集失败：' + s.error) : '没有数据');
    return;
  }
  HW.shown = true;

  // GPU
  setText('hw-gpu-name', (s.gpu_model || '—').replace(/^Tesla |^NVIDIA /, ''));
  setText('hw-gpu-util', s.gpu ?? '—');
  setText('hw-gpu-temp', s.gpu_temp ?? '—');
  setText('hw-gpu-power', (s.gpu_power_w ?? '—') + ' / ' + (s.gpu_power_limit_w ?? '—'));
  spark('hw-gpu-spark', s.gpu_hist, HW_COL.gpu, 100);

  // 显存
  const vu = Number(s.vram_used_gb || 0);
  const vt = Number(s.vram_total_gb || 0);
  const vp = vt ? Math.round(vu / vt * 100) : 0;
  setText('hw-vram-used', vu.toFixed(1));
  setText('hw-vram-total', vt.toFixed(0));
  setText('hw-vram-frac', vp + '%');
  setText('hw-vram-note', vp > 85 ? '接近上限' : '余量 ' + (vt - vu).toFixed(1) + ' GB');
  spark('hw-vram-spark', s.vram_hist, HW_COL.vram, vt || 32);
  $('hw-vram-spark')?.closest('.hw-card')?.classList.toggle('warn', vp > 90);

  // 内存
  const ru = Number(s.ram_used_gb || 0);
  const rt = Number(s.ram_total_gb || 0);
  const rp = rt ? Math.round(ru / rt * 100) : 0;
  setText('hw-ram-used', ru.toFixed(1));
  setText('hw-ram-total', rt.toFixed(0));
  setText('hw-ram-frac', rp + '%');
  setText('hw-ram-note', rp > 85 ? '接近上限' : '余量 ' + (rt - ru).toFixed(1) + ' GB');
  spark('hw-ram-spark', s.ram_hist, HW_COL.ram, rt || 32);
  $('hw-ram-spark')?.closest('.hw-card')?.classList.toggle('warn', rp > 90);

  // CPU
  setText('hw-cpu-name', (s.cpu_model || '—').replace(/\s*(Processor|CPU)\s*$/i, ''));
  setText('hw-cpu-util', s.cpu ?? '—');
  setText('hw-cpu-cores', s.cpu_cores ?? '—');
  setText('hw-cpu-freq', s.cpu_freq_mhz ?? '—');
  spark('hw-cpu-spark', s.cpu_hist, HW_COL.cpu, 100);

  setText('hw-stamp', new Date().toLocaleTimeString('zh-CN', { hour12: false }));
}

function hwStart() {
  if (HW.timer) return;
  hardware();
  HW.timer = setInterval(hardware, 2000);
}
function hwStop() {
  if (HW.timer) { clearInterval(HW.timer); HW.timer = 0; }
}

hwStart();   // 首屏就是 overview，直接开监控（必须在 const HW 之后，否则 TDZ）

// ── 下载链路 / 网络加速 ─────────────────────────────────────
// 后端：GET /api/netboost 测速+检测，POST /api/netboost 启动，
//       GET /api/netboost/pack 查安装包，POST /api/netboost/install 下载安装
async function netProbe() {
  const btn = $('net-probe');
  if (btn) { btn.disabled = true; btn.textContent = '测速中…'; }
  setText('net-msg', '');
  try {
    const r = await api('netboost');
    if (!r.ok) { setText('net-stamp', '采集失败'); setText('net-advice', r.error || '—'); return; }
    const sp = (r.speed || {});
    setText('net-speed', sp.ok ? sp.mbps.toFixed(2) + ' MB/s' : '测不到');
    setText('net-advice', r.advice || '');
    setText('net-stamp', new Date().toLocaleTimeString('zh-CN', { hour12: false }));
    const spp = r.steampp || {};
    setText('net-msg', spp.installed
      ? ('Steam++ 已装：' + (spp.running ? '运行中' : '未运行'))
      : 'Steam++ 未装，可以点「下载并安装」');
    const lb = $('net-launch'); if (lb) lb.disabled = !spp.installed || spp.running;
  } catch (e) {
    setText('net-stamp', '失败');
    setText('net-advice', '测速失败：' + (e && e.message ? e.message : e));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '再测一次'; }
  }
}

$('net-probe')?.addEventListener('click', netProbe);

$('net-launch')?.addEventListener('click', async () => {
  setText('net-msg', '正在启动 Steam++…');
  try {
    const r = await api('netboost', {});
    setText('net-msg', r.ok ? (r.already ? 'Steam++ 已经在运行' : '已启动，等它连上再测一次速度') : ('启动失败：' + r.error));
  } catch (e) { setText('net-msg', '启动失败：' + e.message); }
});

$('net-install')?.addEventListener('click', async () => {
  const pack = await api('netboost/pack').catch(() => null);
  if (!pack || !pack.ok) { setText('net-msg', '查不到安装包：' + (pack && pack.error || '网络问题')); return; }
  const yes = confirm(
    '要下载并安装 Steam++ 吗？\n\n' +
    '版本 ' + pack.version + '，约 ' + pack.size_mb + ' MB。\n' +
    '它会静默安装到本机（不弹安装向导）。\n' +
    'Steam++ 是开源的网络加速工具（github.com/BeyondDimension/SteamTools）。'
  );
  if (!yes) return;
  setText('net-msg', '已提交安装任务，进度看页面底部的日志…');
  await action('netboost/install', {});
});

// ══ API 密钥管理（生成 / 撤销 / 删除）══
// 安全点：
//   · 明文只在 issue 的响应里出现一次，本函数只把它写进 DOM 一次，不缓存、不写外部
//   · 列表里永远不显示明文
//   · 删除/撤销都要二次确认（删除不可恢复）
async function refreshKeys(){
  const box = $('key-list');
  if(!box) return;
  try{
    const s = await api('keys');
    const rows = s.keys || [];
    if(s.weak){
      $('key-msg').textContent = '⚠️ 当前加密后端是弱兜底（非 Windows），密钥只做了掩码，不是真加密。';
    }
    if(!rows.length){
      box.innerHTML = '<p class="muted">还没有密钥。填个备注，点「生成密钥」。</p>';
      return;
    }
    box.innerHTML = `<div class="key-table">${
      rows.map(k=>{
        const state = k.revoked ? '已撤销' : (k.expired ? '已过期' : '有效');
        const cls = (k.revoked||k.expired) ? 'muted' : '';
        return `<div class="key-row ${cls}" data-id="${esc(k.id)}">
          <code>${esc(k.id)}</code>
          <span class="key-label">${esc(k.label||'—')}</span>
          <span class="key-state">${state}</span>
          <span class="key-meta">${esc(k.expires_at||'永久')} · ${k.calls} 次${k.last_ip?' · '+esc(k.last_ip):''}</span>
          <span class="key-ops">
            ${(!k.revoked && !k.expired) ? `<button data-op="revoke" data-id="${esc(k.id)}">撤销</button>` : ''}
            <button class="danger" data-op="delete" data-id="${esc(k.id)}">删除</button>
          </span>
        </div>`;
      }).join('')
    }</div>`;
  }catch(e){ box.innerHTML = '<p class="muted">读取失败：'+esc(e.message||e)+'</p>'; }
}

$('key-issue')?.addEventListener('click', async ()=>{
  const label = ($('key-label')?.value || '').trim() || 'default';
  const ttl = parseInt($('key-ttl')?.value || '90', 10);
  $('key-msg').textContent = '正在生成…';
  try{
    const r = await api('keys', {action:'issue', label, ttl});
    $('key-new').innerHTML = `已生成（<b>只显示这一次</b>，请现在复制）：<br><code class="key-plain">${esc(r.token)}</code><br><span class="muted">id ${esc(r.id)} · 有效至 ${esc(r.expires_at||'永久')}</span>`;
    $('key-msg').textContent = '';
    $('key-label').value = '';
    refreshKeys();
  }catch(e){ $('key-msg').textContent = '生成失败：'+(e.message||e); }
});

$('key-list')?.addEventListener('click', async (ev)=>{
  const btn = ev.target.closest('button[data-op]');
  if(!btn) return;
  const id = btn.dataset.id, op = btn.dataset.op;
  if(op === 'revoke'){
    if(!confirm(`撤销 ${id}？\n撤销后立刻无法调用，但记录保留（便于查谁调用过）。`)) return;
  } else {
    if(!confirm(`删除 ${id}？\n记录会从本机彻底消失，不可恢复。`)) return;
  }
  btn.disabled = true;
  try{
    await api('keys', {action: op, id});
    $('key-msg').textContent = op === 'revoke' ? '已撤销' : '已删除';
    refreshKeys();
  }catch(e){ $('key-msg').textContent = '操作失败：'+(e.message||e); btn.disabled = false; }
});

refreshKeys();

// ══ 一键开始：先显示这台机器现在能干什么，再一键落到最快路径 ══
async function refreshQuick(){
  const box = $('qs-stages'), sub = $('qs-sub');
  if(!box) return;
  try{
    const s = await api('firstrun');
    const st = s.stages || {};
    const order = [['chat','对话'],['image','出图'],['mesh','建模']];
    box.innerHTML = order.map(([k,label])=>{
      const v = st[k] || {};
      return `<span class="qs-chip ${v.ready?'on':''}">${v.ready?'✓':'✗'} ${label}
        <em>${esc((v.how||'').slice(0,42))}</em></span>`;
    }).join('');
    // 说明"最快能怎样"
    const fastest = (s.candidates||[]).find(c=>c.ok && c.stages && c.stages.length);
    if(fastest){
      sub.textContent = fastest.gb > 0
        ? `最快路径：${fastest.name}（要下载约 ${fastest.gb} GB）`
        : `最快路径：${fastest.name}（0 下载，直接可用）`;
    }else{
      sub.textContent = '没有能直接生成的后端 —— 可以先开界面，再在「安装与环境」里补模型。';
    }
    window.__qs = s;
  }catch(e){ sub.textContent = '检查失败：' + (e.message||e); }
}

$('qs-go')?.addEventListener('click', async ()=>{
  const btn = $('qs-go'), msg = $('qs-msg');
  btn.disabled = true;
  msg.textContent = '正在配置…';
  try{
    const r = await api('firstrun', {mode:'auto', launch:true});
    const healed = (r.healed||[]).map(h=>`${h.stage}: ${h.from} → ${h.to}`).join('；');
    if(r.download_started){
      // 下载已经开起来了 —— 就地显示进度，别让用户再去别的页面点一次
      msg.textContent = `${r.name}：开始下载约 ${r.gb} GB，界面现在就能用`;
      pollQuickDownload();
      return;
    }
    msg.textContent = r.need_download
      ? `${r.name}：要下载约 ${r.gb} GB`
      : `已用「${r.name}」启动${healed ? ' · 自动修正 '+healed : ''}`;
    if(!r.need_download){
      setTimeout(()=>{ location.hash='#create'; location.reload(); }, 900);
    }else{
      btn.disabled = false;
    }
  }catch(e){
    msg.textContent = '失败：' + (e.message||e);
    btn.disabled = false;
  }
});

// 下载进度就地显示；下完自动应用配置并进入创作空间
let _qsTimer = null;
function pollQuickDownload(){
  const msg = $('qs-msg');
  clearInterval(_qsTimer);
  _qsTimer = setInterval(async ()=>{
    try{
      const s = await api('status'), j = s.job || {};
      if(j.status === 'running' || j.status === 'queued'){
        const pct = (j.percent||0).toFixed(0);
        const eta = j.eta_sec ? ` · 约 ${Math.ceil(j.eta_sec/60)} 分钟` : '';
        msg.textContent = `${j.label||'下载中'} ${pct}%${eta}`;
        return;
      }
      clearInterval(_qsTimer);
      if(j.status === 'failed'){
        msg.textContent = '下载失败：' + (j.logs||[]).slice(-1)[0] || '看「安装与环境」的日志';
        const b = $('qs-go'); if(b) b.disabled = false;
        return;
      }
      // 成功 → 再跑一次 auto，这次不需要下载了，直接应用 + 启动
      msg.textContent = '下载完成，正在启用…';
      const r = await api('firstrun', {mode:'auto', launch:true});
      msg.textContent = `已用「${r.name}」启动`;
      setTimeout(()=>{ location.hash='#create'; location.reload(); }, 1000);
    }catch(e){ /* 轮询失败继续等下一轮 */ }
  }, 2500);
}

refreshQuick();

// ══ 连接自检 ══
// 用户反馈"点了没反应"，一半情况其实是**页面和 Studio 失联了**（服务被关、
// 或换了个端口）。这里周期性探活，一旦断了就在顶栏明说，而不是让按钮静默失效。
let _pingFails = 0;
async function ping(){
  try{
    const r = await fetch('/api/ping', {cache:'no-store'});
    if(!r.ok) throw new Error('HTTP '+r.status);
    _pingFails = 0;
    document.body.classList.remove('offline');
    return true;
  }catch(e){
    if(++_pingFails >= 2){
      document.body.classList.add('offline');
      try{ $('top-status').textContent = '与 Studio 断开 · 请刷新或重启'; }catch(_){}
    }
    return false;
  }
}
ping(); setInterval(ping, 5000);

// ══ 创作空间加载遮罩 ══
// iframe 里是 Gradio，首屏要 10–20 秒且默认是浅灰骨架 —— 用户看到一片灰就以为
// "点了没反应"。这里盖一层自家深色遮罩，等 iframe 的 load 事件再撤；
// 超过 40 秒还没好就把"在新窗口打开"露出来（有时嵌在 iframe 里被浏览器限制）。
(function(){
  const w = document.getElementById('ws-wrap');
  const f = document.getElementById('workspace');
  const box = document.getElementById('ws-loading');
  const hint = document.getElementById('ws-hint');
  const open = document.getElementById('ws-open');
  if(!w || !f || !box) return;
  let timer = null;
  f.addEventListener('load', ()=>{
    if(!f.getAttribute('src')) return;
    box.hidden = true;
    if(timer){ clearTimeout(timer); timer = null; }
  });
  window.__wsWait = function(url){
    w.hidden = false;
    box.hidden = false;
    hint.textContent = '';
    open.hidden = true;
    open.onclick = ()=> window.open(url || f.getAttribute('src'), '_blank', 'noopener');
    f.hidden = false;
    if(timer) clearTimeout(timer);
    timer = setTimeout(()=>{
      if(!box.hidden){
        hint.textContent = '加载比平时慢（这机器首次启动会读模型）。可以直接在新窗口打开。';
        open.hidden = false;
      }
    }, 40000);
  };
})();

// 深链接（放到最后执行）：#models / #setup / #create 直接落到对应页
(function(){
  try{
    const h=(location.hash||'').replace('#','');
    if(h && document.getElementById(h)) page(h);
  }catch(e){ console.warn('deep-link skipped', e); }
})();
