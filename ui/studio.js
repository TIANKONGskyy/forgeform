const $=id=>document.getElementById(id);let selected='',lastScan=0,lastAction=null,launchPending=false,models=[];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(s){$('toast').textContent=s;$('toast').hidden=false;clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>$('toast').hidden=true,6500)}
async function api(path,data){const r=await fetch('/api/'+path,{method:data===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-Studio-Token':window.STUDIO_TOKEN},body:data===undefined?undefined:JSON.stringify(data)});const j=await r.json();if(!r.ok)throw Error(j.error||'请求失败');return j}
function page(id){document.querySelectorAll('.page').forEach(e=>e.hidden=e.id!==id);document.querySelectorAll('nav button').forEach(e=>e.classList.toggle('active',e.dataset.page===id));$('breadcrumb').textContent=({overview:'工作台',setup:'安装与环境',models:'模型与配置',create:'创作空间'})[id];if(id==='models'){catalog();refreshDelModel();}}
document.querySelectorAll('[data-page]').forEach(b=>b.addEventListener('click',()=>page(b.dataset.page)));
$('theme').onclick=()=>{document.body.classList.toggle('light');localStorage.setItem('forgeform-theme',document.body.classList.contains('light')?'light':'dark')};if(localStorage.getItem('forgeform-theme')==='light')document.body.classList.add('light');
async function action(name,data={}){try{if(name!=='cancel')lastAction={name,data};await api(name,data);await poll()}catch(e){toast(e.message)}}
$('scan').onclick=()=>action('scan',{model_root:$('model-root').value});$('runtime').onclick=()=>action('runtime');$('install').onclick=()=>{if(selected)action('install',{key:selected})};$('cancel').onclick=()=>action('cancel');$('launch').onclick=()=>{launchPending=true;action('launch')};
function renderScan(s){if(!s||s.scanned_at===lastScan)return;lastScan=s.scanned_at;selected=s.default;$('model-root').value=s.model_root;const h=s.hardware;$('hardware').innerHTML=[['显卡',h.gpu_name||'未检测到 NVIDIA',`${h.vram_gb} GB 显存 · 空闲 ${h.vram_free_gb} GB`],['系统内存',`${h.ram_gb} GB`,'加载峰值需额外余量'],['可用磁盘',`${h.disk_free_gb} GB`,'按模型保存位置检测'],['已有环境',s.frameworks.filter(f=>f.alive).length+' 个','只读探测，不预热模型']].map(([a,b,c])=>`<article><small>${esc(a)}</small><strong>${esc(b)}</strong><p>${esc(c)}</p></article>`).join('');$('frameworks').innerHTML=s.frameworks.map(f=>`<span class="${f.alive?'found':''}" title="${esc(f.detail||f.note)}">${f.alive?'●':'○'} ${esc(f.label)}${f.alive?' · '+f.models.length+' 个模型':''}</span>`).join('');$('scan-time').textContent='刚刚检测';$('plans').innerHTML=s.profiles.map(p=>`<button class="plan ${p.key===selected?'selected':''}" data-plan="${esc(p.key)}" ${!p.fits?'disabled':''}><span class="tag">${p.key===s.default?'推荐方案':p.fits?'可选方案':'当前不适合'}</span><h3>${esc(p.name)}</h3><p>${esc(p.desc)}</p><div class="size">${p.download_gb} <small>GB 下载</small></div><p>${esc(p.reason)}</p>${(p.quant_hints||[]).map(h=>`<div class="hint">${esc(h.model)} · ${esc(h.quant)}${h.offload?' / 内存卸载':''}<br>预计显存 ${h.vram_need_gb} GB · 内存 ${h.ram_need_gb} GB${h.fits?'':' · 资源需复核'}</div>`).join('')}</button>`).join('')||'<div class="empty">没有找到可用方案。可在模型与配置中接入已有服务。</div>';$('plans').querySelectorAll('[data-plan]').forEach(b=>b.onclick=()=>{selected=b.dataset.plan;$('plans').querySelectorAll('.plan').forEach(p=>p.classList.toggle('selected',p===b));$('install').disabled=false});$('install').disabled=!selected;}
async function catalog(){try{const s=await api('catalog');models=s.models;for(const [id,kind] of [['image','image'],['mesh','3d']]){const e=$(id);const current=s.config.stages[kind]?.default_model||'';const options=models.filter(p=>p.kind===kind);e.innerHTML='<option value="">保持当前模型</option>'+options.map(p=>`<option value="${esc(p.id)}">${esc(p.label)}${p.installed?' · 本地已有':''}</option>`).join('');if(options.some(p=>p.id===current))e.value=current;e.onchange=()=>{$(id+'_path').value=options.find(p=>p.id===e.value)?.path||''};e.onchange();}$('mode').value=s.config.backend.mode||'auto';$('catalog').innerHTML=models.map(p=>`<div class="catalog-item"><b>${esc(p.label)}</b><p class="${p.installed?'installed':''}">${p.installed?'本地已有':'待安装'} · ${esc(p.kind)}</p><p>${esc(p.path||p.repo)}</p></div>`).join('')}catch(e){toast(e.message)}}
$('settings').onsubmit=async e=>{e.preventDefault();try{const r=await api('config',{mode:$('mode').value,quant:$('quant').value,image:$('image').value,'3d':$('mesh').value,image_path:$('image_path').value,'3d_path':$('mesh_path').value,llm:$('llm').value,endpoint:$('endpoint').value});$('save-result').textContent=r.message;toast('配置已保存')}catch(e){$('save-result').textContent=e.message;toast(e.message)}};
async function poll(){try{const s=await api('status'),j=s.job;renderScan(s.snapshot);$('top-status').textContent=j.status==='running'?j.label:s.runtime_ready?'环境已安装':'待准备环境';$('job').hidden=j.status==='idle';$('job-title').textContent=j.label;$('job-kind').textContent=({running:'正在进行',done:'完成',failed:'未完成 · 可重试',cancelled:'已取消'})[j.status]||'';$('logs').textContent=j.logs.join('\n');$('cancel').hidden=j.status!=='running';if(j.total){$('progress').max=j.total;$('progress').value=j.step;$('job-step').textContent=`已完成 ${j.step} / ${j.total} 个步骤`}else{$('progress').removeAttribute('value');$('job-step').textContent=j.status==='running'?'正在处理，详细记录会持续更新。':''}
// 下载中的文件级进度：只显示"第几步"时，30 GB 的模型看着像卡死
if(j.status==='running'&&j.file){$('download').hidden=false;$('download-name').textContent=j.file;$('download-pct').textContent=(j.percent||0).toFixed(1)+'%';{const eta=j.eta_sec||0;const etaTxt=eta>0?` · 剩余约 ${eta>=60?Math.floor(eta/60)+' 分 '+(eta%60)+' 秒':eta+' 秒'}`:'';
$('download-detail').textContent=`${(j.downloaded_mb||0).toFixed(0)} / ${(j.size_mb||0).toFixed(0)} MB · ${(j.speed_mbps||0).toFixed(2)} MB/s${etaTxt}`};$('download-bar').style.width=Math.min(100,j.percent||0)+'%'}else{$('download')&&($('download').hidden=true)}for(const id of ['scan','runtime','launch'])$(id).disabled=j.status==='running';$('install').disabled=j.status==='running'||!selected;if(launchPending&&j.status==='done'&&s.ready){$('workspace').src=s.app_url+'/?__theme='+ (document.body.classList.contains('light')?'light':'dark');$('workspace').hidden=false;$('create-empty').hidden=true;launchPending=false}if(j.status==='failed'||j.status==='cancelled'){launchPending=false;if(!$('retry')){const b=document.createElement('button');b.id='retry';b.textContent='重试此任务';b.onclick=()=>{if(lastAction)action(lastAction.name,lastAction.data)};$('job').append(b)}}else $('retry')?.remove()}catch(e){$('top-status').textContent='连接中断';if(!window.connectionError){toast('本地服务连接中断，请重新打开启动程序');window.connectionError=true}}}
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
      ? installed.map(m => `<option value="${m.id}">${m.label}（${m.kind}）</option>`).join('')
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
      ? plan.items.map(i => `<div class="purge-item"><b>${i.label}</b>
          <span>${i.size_gb} GB</span>
          <small>${i.note || ''}</small>
          <code>${i.path}</code></div>`).join('')
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
