(() => {
'use strict';
const root = document.getElementById('task-workbench');
const $ = s => root.querySelector(s);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icons = {
  terminal:'<path d="m4 6 5 6-5 6m8 0h8"/>',
  file:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M8 13h8m-8 4h6"/>',
  columns:'<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18m6-18v18"/>',
  list:'<path d="M9 6h12M9 12h12M9 18h12M3 6h.01M3 12h.01M3 18h.01"/>',
  arrow:'<path d="M7 17 17 7M7 7h10v10"/>',
  link:'<path d="m10 13 4-4M8 16l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 1 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0"/>',
  bot:'<rect x="3" y="7" width="18" height="14" rx="3"/><path d="M12 3v4M8 12h.01M16 12h.01M8 17h8"/>',
};
const icon = n => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[n] || icons.file}</svg>`;
const statuses = {queued:'待开始', running:'进行中', review:'待你处理', done:'已完成', failed:'失败', cancelled:'已取消'};
const statusOrder = ['queued','running','review','done','failed','cancelled'];
const drafts = new Map();
let state = {projects:[], tasks:[], nodes:[], trajectory:[], deletedProjects:[], adapters:[], workers:[], maxWorkers:3, version:null};
let page = 'board', selected = null, project = 'all', filter = 'all', view = 'board';
let selectedNode = null, nodeFilter = 'all', colorFilter = 'all', textFilter = '';
let composingText = false, renderAfterComposition = false;
let loaded = false, connected = false, busy = false, notice = '', noticeError = false;
let fetchSequence = 0, preview = null, renderedKey = null;
try {
  const saved = JSON.parse(localStorage.getItem('taskboard.view') || '{}');
  project = typeof saved.project === 'string' ? saved.project : 'all';
  filter = ['all','attention','artifacts'].includes(saved.filter) ? saved.filter : 'all';
  view = saved.view === 'list' ? 'list' : 'board';
  nodeFilter = typeof saved.nodeFilter === 'string' ? saved.nodeFilter : 'all';
  colorFilter = typeof saved.colorFilter === 'string' ? saved.colorFilter : 'all';
} catch (_) { /* Browser storage is optional. */ }
const tasks = () => state.tasks;
const taskFor = id => tasks().find(t => t.id === id);
const currentTask = () => taskFor(selected);
const projectFor = id => state.projects.find(p => p.id === id);
const projectName = id => projectFor(id)?.name || state.deletedProjects.find(p => p.id === id)?.name || '未归类';
const adapterFor = id => state.adapters.find(a => a.id === id);
const agentName = id => adapterFor(id)?.name || id || '未选择';
const arr = value => Array.isArray(value) ? value : value ? [String(value)] : [];
const artifacts = t => arr(t.artifacts);
const steps = t => arr(t.steps);
const attention = t => Boolean(t.cancelFailed) || ['review','failed'].includes(t.status);
const projectTasks = () => project === 'all' ? tasks() : tasks().filter(t => t.projectId === project);
const visibleTasks = () => projectTasks().filter(taskMatchesFilters);
const timeLabel = value => {
  if (!value) return '—';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
};
function safeActionUrl(value) {
  if (typeof value !== 'string') return '';
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : '';
  } catch (_) { return ''; }
}
function questionAttention(t) {
  if (t.status !== 'review' || t.kind !== 'question') return null;
  const data = t.attention && typeof t.attention === 'object' ? t.attention : {};
  const text = value => typeof value === 'string' ? value.trim() : '';
  const latestLog = arr(t.log).slice().reverse().map(entry => text(entry?.[1])).find(Boolean) || '';
  const fallback = text(t.result) || latestLog;
  const description = text(data.description) || fallback;
  return {
    badge:text(data.badge) || '需要你处理',
    title:text(data.title) || description.split('\n').find(line => line.trim()) || '尚未收到具体操作说明',
    description:description || '请在当前协作对话中要求负责此任务的 worker 补充具体问题与下一步操作。',
    actionLabel:text(data.actionLabel) || '打开操作页面',
    actionUrl:safeActionUrl(data.actionUrl),
  };
}
const badge = t => `<span class="tw-badge ${esc(statusOrder.includes(t.status) ? t.status : '')}">${esc(t.cancelFailed ? '等待确认 Agent 退出' : t.cancelRequested ? '正在停止' : t.status === 'review' ? (t.kind === 'plan' ? '待确认计划' : t.kind === 'question' ? questionAttention(t).badge : '待验收') : statuses[t.status] || t.status)}</span>`;
const projectOptions = id => state.projects.map(p => `<option value="${esc(p.id)}"${p.id === id ? ' selected' : ''}>${esc(p.name)}</option>`).join('');
const agentOptions = id => state.adapters.map(a => `<option value="${esc(a.id)}"${a.id === id ? ' selected' : ''}${!a.enabled || !a.available ? ' disabled' : ''}>${esc(a.name)}${!a.enabled ? '（已停用）' : !a.available ? '（未找到）' : ''}</option>`).join('');
const criteriaHtml = criteria => arr(criteria).length ? `<ul class="tw-criteria">${arr(criteria).map(c => `<li>${esc(c)}</li>`).join('')}</ul>` : '<p class="tw-small">尚未填写验收标准。</p>';
const actionButton = (action, label, primary = false) => `<button type="button" class="tw-btn${primary ? ' tw-primary' : ''}${action === 'cancel' ? ' tw-danger' : ''}" data-action="${action}"${busy ? ' disabled' : ''}>${label}</button>`;
function saveView() {
  try { localStorage.setItem('taskboard.view', JSON.stringify({project, filter, view, nodeFilter, colorFilter})); } catch (_) {}
}
function setNotice(text = '', error = false) {
  notice = text; noticeError = error;
  const el = $('#tw-message');
  el.textContent = text; el.hidden = !text; el.classList.toggle('error', error);
  el.setAttribute('role', error ? 'alert' : 'status');
}
function connectionState(ok) {
  connected = ok;
  const el = $('#tw-connection');
  el.className = `tw-connection ${ok ? 'online' : 'offline'}`;
  el.innerHTML = `<span class="tw-dot"></span>本机工作台 · ${ok ? '已连接' : '连接中断'}`;
  if (ok) $('#tw-last-sync').textContent = '同步于 ' + new Date().toLocaleTimeString('zh-CN', {hour12:false});
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    cache:'no-store', ...options,
    headers:{'Content-Type':'application/json','X-Taskboard-Client':'taskboard', ...(options.headers || {})},
  });
  let data;
  try { data = await response.json(); } catch (_) { throw new Error(`服务响应无法读取（${response.status}）。`); }
  if (!response.ok) throw new Error(data.error || `操作未完成（${response.status}）。`);
  return data;
}
async function refresh(force = false) {
  const sequence = ++fetchSequence;
  try {
    const next = await api('/api/state');
    if (sequence !== fetchSequence) return;
    const changed = !loaded || force || next.version !== state.version || JSON.stringify(next.workers) !== JSON.stringify(state.workers);
    state = {...state, ...next};
    if (!Array.isArray(state.tasks) || !Array.isArray(state.projects)) throw new Error('服务返回的任务数据不完整。');
    if (!loaded && project === 'all' && state.projects.length === 1) project = state.projects[0].id;
    if (project !== 'all' && !projectFor(project)) { project = state.projects[0]?.id || 'all'; nodeFilter = 'all'; selectedNode = null; }
    if (!['all','unassigned'].includes(nodeFilter) && !nodesInProject().some(n => n.id === nodeFilter)) nodeFilter = 'all';
    loaded = true; connectionState(true);
    if (changed) render();
  } catch (error) {
    if (sequence !== fetchSequence) return;
    connectionState(false);
    if (!loaded) $('#tw-content').innerHTML = `<div class="tw-loader"><h3>暂时无法连接本机服务</h3><p style="margin-top:10px">${esc(error.message)}</p><button type="button" data-refresh class="tw-btn" style="margin-top:18px">重新连接</button></div>`;
  }
}
function captureForm() {
  const key = renderedKey;
  const values = {};
  $('#tw-content').querySelectorAll('input[id],textarea[id],select[id]').forEach(el => {
    values[el.id] = el.multiple ? Array.from(el.selectedOptions).map(o => o.value) : el.type === 'checkbox' ? el.checked : el.value;
  });
  if (key && Object.keys(values).length) drafts.set(key, values);
  const active = document.activeElement;
  return {
    key, id:root.contains(active) ? active.id : null,
    start:active?.selectionStart, end:active?.selectionEnd,
    scroll:window.scrollY, inner:$('#tw-content .tw-logs')?.scrollTop || 0,
    graphs:Array.from(root.querySelectorAll('.tw-graph-scroll')).map(el => [el.dataset.project,el.scrollLeft,el.scrollTop]),
    openHistory:Array.from(root.querySelectorAll('details[data-history-id][open]')).map(el=>el.dataset.historyId),
  };
}
function restoreForm(snapshot) {
  const values = drafts.get(draftKey()) || {};
  Object.entries(values).forEach(([id, value]) => {
    const el = document.getElementById(id);
    if (!el || !root.contains(el)) return;
    if (el.multiple) Array.from(el.options).forEach(o => { o.selected = arr(value).includes(o.value); });
    else if (el.type === 'checkbox') el.checked = value;
    else if (el.tagName !== 'SELECT' || Array.from(el.options).some(o => o.value === value && !o.disabled)) el.value = value;
  });
  if (snapshot?.key !== draftKey()) return;
  const el = snapshot.id ? document.getElementById(snapshot.id) : null;
  if (el && root.contains(el)) {
    el.focus({preventScroll:true});
    if (typeof snapshot.start === 'number' && ['INPUT','TEXTAREA'].includes(el.tagName)) {
      try { el.setSelectionRange(snapshot.start, snapshot.end); } catch (_) {}
    }
  }
  for (const el of root.querySelectorAll('details[data-history-id]')) el.open = (snapshot.openHistory || []).includes(el.dataset.historyId);
  for (const [id,left,top] of snapshot.graphs || []) { const graph = Array.from(root.querySelectorAll('.tw-graph-scroll')).find(el => el.dataset.project === id); if (graph) { graph.scrollLeft = left; graph.scrollTop = top; } }
  const log = $('#tw-content .tw-logs');
  if (log) log.scrollTop = snapshot.inner;
  window.scrollTo({top:snapshot.scroll, behavior:'instant'});
}
function navigate(nextPage, id = selected) {
  captureForm(); page = nextPage; selected = id; setNotice(''); render();
  window.scrollTo({top:0, behavior:'instant'});
}
function header() {
  const visible = visibleTasks();
  const selector = $('#tw-project-filter');
  selector.innerHTML = `<option value="all">所有项目 · ${state.projects.length}</option>${projectOptions(project)}<option value="__new__">＋ 新建项目</option>`;
  selector.value = project;
  const external = tasks().filter(t => t.status === 'running' && t.executionMode === 'external');
  const local = tasks().filter(t => t.status === 'running' && t.executionMode !== 'external');
  const workerNames = [...new Set(external.map(t => t.worker).filter(Boolean))];
  const count = workerNames.length + local.length;
  const unconfirmed = tasks().filter(t => t.cancelFailed).length;
  $('#tw-workers').innerHTML = `<span class="tw-flex" style="gap:3px">${Array.from({length:Math.min(Math.max(count, 1), 3)}, (_, i) => `<span class="tw-avatar ${count > i ? 'busy' : ''}">${i + 1}</span>`).join('')}</span><span>${count} 位 worker 工作中${unconfirmed ? ' · ' + unconfirmed + ' 位退出待核实' : ''}</span>`;
  $('#tw-navigation').innerHTML = `<div class="tw-tabs"><button class="tw-tab" data-filter="all" aria-pressed="${filter === 'all'}">全部任务 <span class="tw-count">${visible.length}</span></button><button class="tw-tab" data-filter="attention" aria-pressed="${filter === 'attention'}">待我处理 <span class="tw-count tw-urgent">${visible.filter(attention).length}</span></button><button class="tw-tab" data-filter="artifacts" aria-pressed="${filter === 'artifacts'}">产物 <span class="tw-count">${visible.reduce((n, t) => n + artifacts(t).length, 0)}</span></button></div><div class="tw-switch" aria-label="任务显示方式"><button data-layout="board" aria-pressed="${view === 'board'}">${icon('columns')}看板</button><button data-layout="list" aria-pressed="${view === 'list'}">${icon('list')}清单</button></div>`;
  renderExplorationNavigation();
  $('#tw-footer-state').textContent = `${visible.filter(t => t.status === 'running').length} 个任务进行中 · ${visible.filter(attention).length} 个需要你处理 · ${visible.filter(t => t.status === 'done').length} 个已完成`;
}
function workerLabel(t) {
  if (t.executionMode === 'external') return t.worker ? `协作 worker · ${t.worker}` : '协作任务';
  return t.worker || agentName(t.engine);
}
function queueLabel(t) {
  if (arr(t.deps).some(id => taskFor(id)?.status !== 'done')) return '等待前序验收';
  if (t.executionMode === 'external') return '等待协作进展';
  return t.scheduled ? '等待空闲 worker' : '尚未启动';
}
function card(t) {
  const next = t.cancelFailed ? {title:'先结束 Agent 并确认写入已停止',description:`请在活动监视器或原 CLI 中结束 ${agentName(t.engine)}${t.processId ? '（PID ' + t.processId + '）' : ''}，确认后打开任务继续处理。`} : questionAttention(t);
  const firstLine = next?.description.split('\n').find(line => line.trim())?.trim() || '';
  const summary = firstLine.length > 120 ? firstLine.slice(0, 120) + '…' : firstLine;
  const nextStep = next ? `<strong>下一步：${esc(next.title)}</strong>${summary && summary !== next.title ? `<div style="margin-top:5px">${esc(summary)}</div>` : ''}` : esc(t.desc || t.goal || '');
  const count = Math.max(0, Math.min(Number(t.done) || 0, steps(t).length));
  return `<button type="button" class="tw-task" data-open="${esc(t.id)}" aria-label="查看任务：${esc(t.title)}"><div class="tw-project"><span>${esc(projectName(t.projectId))}</span><span>${esc(t.id)}</span></div><div>${badge(t)}<div class="tw-tasktitle">${esc(t.title)}</div>${nodeChip(nodeFor(t.nodeId))}</div><div><div class="tw-engine">${icon('terminal')}${esc(t.executionMode === 'external' ? '协作 worker' : agentName(t.engine))}</div><div class="tw-taskdesc">${nextStep}</div>${steps(t).length ? `<div class="tw-stepbar" aria-label="已完成 ${count} 项，共 ${steps(t).length} 项">${steps(t).map((_, i) => `<span class="${i < count ? 'filled' : ''}"></span>`).join('')}</div><div class="tw-stepcaption">${count}/${steps(t).length} 项已确认完成</div>` : ''}</div><div class="tw-cardfoot"><span class="tw-flex" style="gap:4px">${icon(t.status === 'queued' ? 'link' : 'bot')}${esc(t.status === 'queued' ? queueLabel(t) : t.worker || (t.status === 'done' ? '已验收' : '查看进展'))}</span><span>${artifacts(t).length ? artifacts(t).length + ' 份产物' : next ? '查看操作 →' : '查看 →'}</span></div></button>`;
}
function board() {
  const visible = visibleTasks();
  if (!state.projects.length) return emptyProjects();
  if (!visible.length && (nodeFilter !== 'all' || colorFilter !== 'all' || textFilter)) return '<div class="tw-zero"><h3>没有符合筛选的任务</h3><p class="tw-small">可以调整节点、颜色或关键词。</p><button class="tw-btn" data-clear-filters>清除筛选</button></div>';
  if (!visible.length) return `<div class="tw-zero"><h3>这个项目还没有任务</h3><p class="tw-small">添加第一个目标，再把它拆成可以执行的任务。</p><button type="button" id="tw-first-task" class="tw-btn tw-primary">＋ 新建任务</button></div>`;
  const filtered = visible.filter(t => filter === 'attention' ? attention(t) : filter === 'artifacts' ? artifacts(t).length : true);
  if (!filtered.length) return `<div class="tw-board"><div class="tw-empty tw-empty-filter">${filter === 'attention' ? '暂时没有需要你处理的任务。' : '还没有可查看的产物。'}<br><button class="tw-link" data-filter="all" style="margin-top:10px">查看全部任务 →</button></div></div>`;
  const order = statusOrder.filter(s => (s !== 'cancelled' || filtered.some(t => t.status === s)) && (filter === 'all' || filtered.some(t => t.status === s)));
  return `<div class="tw-board${view === 'list' ? ' list' : ''}${filter !== 'all' ? ' filtered' : ''}">${order.map(s => `<section class="tw-column" aria-label="${statuses[s]}"><div class="tw-colhead"><span class="tw-flex"><span class="tw-dot ${s}"></span>${statuses[s]}</span><span class="tw-count">${filtered.filter(t => t.status === s).length}</span></div>${filtered.filter(t => t.status === s).map(card).join('') || '<div class="tw-empty">暂无任务</div>'}</section>`).join('')}</div>`;
}
function artifactList(t) {
  return artifacts(t).map((a, i) => `<button type="button" class="tw-artifact" data-artifact="${i}" data-task="${esc(t.id)}"><span class="tw-flex">${icon('file')}<span>${esc(a.name)}<span class="tw-subnote" style="display:block">${esc(a.type || '文件')} · 点击预览</span></span></span>${icon('arrow')}</button>`).join('');
}
function detailCallout(t) {
  let title = '', description = '', buttons = '';
  const external = t.executionMode === 'external';
  if (t.cancelFailed) {
    title = '尚未确认 Agent 退出，请先处理原进程。';
    const alive = t.processState === 'alive';
    description = `1. 在活动监视器或原 CLI 中找到 ${agentName(t.engine)}${t.processId ? '（记录的 PID ' + t.processId + '）' : ' 的对应任务进程（未记录 PID）'}，先核对确实属于这个任务。\n2. 结束该 Agent，并确认它已退出、对项目文件的写入已停止。\n3. ${alive ? '等待工作台核实进程退出，状态会自动更新。' : '完成后点击下方确认按钮，再决定是否重试。'}\n\n${alive ? '当前仍检测到该 PID 存在，暂不能人工确认。PID 可能被复用，请勿只凭数字结束不明进程。' : '确认按钮不会结束进程。服务会再次核对已记录的 PID；进程仍存在时会拒绝确认。'}`;
    if (!alive) buttons = actionButton('confirm_stopped', '我已确认 Agent 退出', true);
  } else if (t.status === 'review' && t.kind === 'plan') {
    title = '计划已生成，请确认任务拆分。';
    description = arr(t.plan).length ? `批准后会创建 ${t.plan.length} 个子任务，并按依赖关系执行。` : '当前结果尚无可执行的任务拆分。请补充反馈，让 Agent 重新整理计划。';
    if (arr(t.plan).length && !external) buttons += actionButton('approve_plan', '批准计划并分发', true);
    buttons += '<button class="tw-btn" data-focus-feedback>调整计划</button>';
  } else if (t.status === 'review' && t.kind === 'question') {
    const next = questionAttention(t);
    title = next.title;
    description = next.description;
    if (next.actionUrl) buttons += `<a class="tw-btn tw-primary" href="${esc(next.actionUrl)}" target="_blank" rel="noopener noreferrer" style="text-decoration:none">${esc(next.actionLabel)} ${icon('arrow')}</a>`;
    buttons += '<button class="tw-btn" data-focus-feedback>补充反馈</button>';
  } else if (t.status === 'review') {
    title = '结果已就绪，等待你的验收。';
    description = external ? '这是本轮协作开发任务；可以查看产物、确认结果或留下反馈。' : '核对产物与验收标准。验收通过后，后续依赖任务才能继续。';
    buttons += actionButton('accept', '验收通过', true);
    buttons += '<button class="tw-btn" data-focus-feedback>补充反馈</button>';
  } else if (t.status === 'failed') {
    title = '这次执行失败，需要决定下一步。';
    description = t.error || '请查看下方执行记录。';
    if (!external) buttons += actionButton('retry', '重试任务', true);
    buttons += '<button class="tw-btn" data-focus-feedback>补充说明</button>';
  } else if (t.status === 'running') {
    title = t.cancelRequested ? '正在停止 Agent。' : t.desc || '任务正在执行。';
    description = t.cancelRequested ? '等待进程退出后即可重试；已有记录与产物会保留。' : external ? '进展由负责此任务的协作 worker 汇报，更新后会自动出现在这里。' : `${agentName(t.engine)} 正在工作，执行记录会持续更新。`;
  } else if (t.status === 'queued') {
    title = queueLabel(t) + '。';
    description = external ? '此任务由当前协作会话推进。' : t.scheduled ? '满足前序依赖且有可用执行位置时自动开始。' : t.phase === 'plan' ? '启动后先生成计划，等你确认后再分发执行。' : '启动后按目标与验收标准执行。';
    if (!external && !t.scheduled) buttons += actionButton('start', t.phase === 'plan' ? '开始生成计划' : '开始执行', true);
  } else if (t.status === 'cancelled') {
    title = '任务已取消。'; description = '历史记录与已有产物仍然保留。';
    if (!external) buttons += actionButton('retry', '重新开始', true);
  } else {
    title = '已完成，可查看交付物和执行记录。'; description = '需要进一步调整时，可以在下方补充反馈。';
  }
  if (!external && !t.cancelRequested && !t.cancelFailed && ['queued','running','review','failed'].includes(t.status)) buttons += actionButton('cancel', '取消任务');
  return `<div class="tw-callout ${esc(statusOrder.includes(t.status) ? t.status : '')}"><strong>${esc(title)}</strong>${description ? `<p style="margin-top:6px;font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere">${esc(description)}</p>` : ''}${buttons ? `<div class="tw-actions">${buttons}</div>` : ''}</div>`;
}
function detail() {
  const t = currentTask();
  if (!t) return '<div class="tw-loader">任务已不存在。<button class="tw-link" data-back="board">返回看板</button></div>';
  const plan = arr(t.plan);
  const children = tasks().filter(child => child.parentTaskId === t.id);
  const feedbackAllowed = !t.cancelFailed && ['review','failed','done'].includes(t.status) && !children.length;
  return `<div class="tw-detail"><div class="tw-detailhead"><button class="tw-link" data-back="board">← 返回任务看板</button><span class="tw-small">${esc(projectName(t.projectId))} / ${esc(t.id)}</span></div><div class="tw-flex tw-between tw-wrap"><h3>${esc(t.title)}</h3>${badge(t)}</div><div class="tw-engine" style="margin-top:10px">${icon('terminal')}${esc(agentName(t.engine))}<span>· ${esc(workerLabel(t))}</span></div><div class="tw-detailgrid"><div>${detailCallout(t)}${taskNodeSection(t)}<section class="tw-section"><h4>目标与验收标准</h4><p style="font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere">${esc(t.goal || t.desc)}</p>${criteriaHtml(t.criteria)}</section>${plan.length ? `<section class="tw-section"><h4>${t.status === 'review' ? '待确认的任务拆分' : '任务计划'}</h4><ol class="tw-steps">${plan.map((p, i) => `<li><span class="tw-n">${i + 1}</span><div><strong>${esc(p.title)}</strong>${p.goal ? `<p class="tw-plan-goal">${esc(p.goal)}</p>` : ''}<div class="tw-subnote">${arr(p.deps).length ? '依赖第 ' + arr(p.deps).map(d => Number(d) + 1).join('、') + ' 项' : '可独立开始'}</div>${criteriaHtml(p.criteria)}</div></li>`).join('')}</ol></section>` : ''}${steps(t).length ? `<section class="tw-section"><h4>已记录的进展</h4><ol class="tw-steps">${steps(t).map((s, i) => `<li class="${i < Number(t.done) ? 'complete' : ''}"><span class="tw-n">${i < Number(t.done) ? '✓' : i + 1}</span><div>${esc(s)}</div></li>`).join('')}</ol></section>` : ''}${arr(t.deps).length ? `<section class="tw-section"><h4>前序任务</h4>${arr(t.deps).map(id => `<button type="button" class="tw-dep" data-open="${esc(id)}">${esc(taskFor(id)?.title || id)} · ${esc(statuses[taskFor(id)?.status] || '未知状态')} ↗</button>`).join('')}</section>` : ''}${children.length ? `<section class="tw-section"><h4>子任务 <span class="tw-count">${children.length}</span></h4>${children.map(child => `<button type="button" class="tw-artifact" data-open="${esc(child.id)}"><span>${esc(child.title)}</span>${badge(child)}</button>`).join('')}</section>` : ''}${t.result ? `<section class="tw-section"><h4>执行结果</h4><div class="tw-result">${esc(t.result)}</div></section>` : ''}${feedbackAllowed ? `<form id="tw-feedback-form" class="tw-section"><label for="tw-feedback">给这个任务反馈</label><textarea id="tw-feedback" name="feedback" placeholder="说明哪些结果符合预期、哪里需要调整。" maxlength="12000" required></textarea><div class="tw-actions" style="margin-top:8px"><button type="submit" class="tw-btn tw-primary"${busy ? ' disabled' : ''}>${t.executionMode === 'external' ? '记录反馈' : '发送并继续'}</button><span class="tw-small">${t.executionMode === 'external' ? '保存到任务记录，由当前协作会话处理' : '保留已有记录，继续同一任务'}</span></div></form>` : ''}</div><aside><section class="tw-section"><h4>交付物 <span class="tw-count">${artifacts(t).length}</span></h4>${artifactList(t) || '<p class="tw-small">尚未生成产物。</p>'}</section><section class="tw-section"><h4>执行记录</h4><div class="tw-logs"><div class="tw-log">${arr(t.log).slice().reverse().map(l => `<div class="tw-logitem"><div class="tw-logtime">${esc(timeLabel(l[0]))}</div>${esc(l[1])}</div>`).join('') || '<p class="tw-small">等待第一条记录。</p>'}</div></div></section>${arr(t.runs).length ? `<section class="tw-section"><h4>执行轮次</h4>${arr(t.runs).slice().reverse().map((run, index) => `<details class="tw-run"><summary>第 ${arr(t.runs).length - index} 次 · ${esc(run.status || (run.ok === true ? '成功' : run.ok === false ? '失败' : '执行记录'))}</summary><pre>${esc(JSON.stringify(run, null, 2))}</pre></details>`).join('')}</section>` : ''}<section class="tw-section"><h4>任务信息</h4><p class="tw-small">创建于 ${esc(timeLabel(t.createdAt))}</p><p class="tw-small">更新于 ${esc(timeLabel(t.updatedAt))}</p>${t.parentTaskId ? `<button class="tw-link" data-open="${esc(t.parentTaskId)}" style="margin-top:8px">查看所属计划 →</button>` : ''}</section></aside></div></div>`;
}
function newTaskForm() {
  if (!state.projects.length) return emptyProjects();
  const selectedProject = drafts.get('new:')?.['tw-task-project'] || (project === 'all' ? state.projects[0]?.id : project);
  const engine = state.adapters.find(a => a.available && a.enabled)?.id;
  const dependencies = tasks().filter(t => t.projectId === selectedProject && t.status !== 'cancelled');
  return `<div class="tw-detail"><button class="tw-link" data-back="board">← 返回任务看板</button><form id="tw-new-form" class="tw-newform"><h3 style="margin-bottom:21px">你想完成什么？</h3><div class="tw-formgroup"><label for="tw-task-project">所属项目</label><select id="tw-task-project" name="projectId" required>${projectOptions(selectedProject)}</select><p class="tw-formhint">任务在此项目的本机目录中执行。</p></div><div class="tw-formgroup"><label for="tw-task-node">探索节点</label><select id="tw-task-node" name="nodeId">${nodeOptions(selectedProject, drafts.get('new:')?.['tw-task-node'] || (nodeFor(nodeFilter)?.projectId === selectedProject ? nodeFilter : ''))}</select><p class="tw-formhint">子任务会继承所属节点；也可以暂时保留为未归类。</p></div><div class="tw-formgroup"><label for="tw-title">任务名称</label><input id="tw-title" name="title" maxlength="160" placeholder="例如：给任务管理器加入搜索功能" required></div><div class="tw-formgroup"><label for="tw-goal">目标和必要背景</label><textarea id="tw-goal" name="goal" maxlength="20000" placeholder="给谁用，解决什么问题，已有些什么？" required></textarea></div><div class="tw-formgroup"><label for="tw-criteria">怎样算完成？</label><textarea id="tw-criteria" name="criteria" maxlength="6000" placeholder="每行一项验收标准，例如：输入关键词，可以找到对应任务。" required></textarea></div><div class="tw-formgroup"><label for="tw-engine">使用哪个 Agent？</label><select id="tw-engine" name="engine">${agentOptions(engine)}</select>${!engine ? '<p class="tw-error">尚无可用 Agent。可以先保存任务，配置完成后再启动。</p>' : '<p class="tw-subnote">使用本机已安装的 Agent；登录与权限按各 Agent 的设置执行。</p>'}</div><div class="tw-formgroup"><label for="tw-phase">执行方式</label><select id="tw-phase" name="phase"><option value="plan">先生成计划，确认后分发</option><option value="execute">目标明确，直接执行</option></select></div>${dependencies.length ? `<div class="tw-formgroup"><label for="tw-deps">前序任务（选填，可多选）</label><select id="tw-deps" name="deps" multiple size="${Math.min(dependencies.length, 4)}">${dependencies.map(t => `<option value="${esc(t.id)}">${esc(t.id + ' · ' + t.title + ' · ' + (statuses[t.status] || t.status))}</option>`).join('')}</select><p class="tw-formhint">只会在所选任务全部验收后开始。</p></div>` : ''}<div class="tw-callout"><h4>先确认方向，再推进任务</h4><p class="tw-subnote">“先生成计划”会等待你的批准；执行完成也需要验收，才会标记为已完成。</p></div><div class="tw-actions"><button type="submit" name="intent" value="start" class="tw-btn tw-primary"${busy || !engine ? ' disabled' : ''}>创建并启动 →</button><button type="submit" name="intent" value="save" class="tw-btn"${busy ? ' disabled' : ''}>先保存任务</button></div></form></div>`;
}
function newProjectForm() {
  return `<div class="tw-detail"><button class="tw-link" data-back="board">← 返回任务看板</button><form id="tw-project-form" class="tw-newform"><h3 style="margin-bottom:20px">新建项目</h3><div class="tw-formgroup"><label for="tw-project-name">项目名称</label><input id="tw-project-name" name="name" maxlength="100" placeholder="一个清楚、便于区分的名字" required></div><div class="tw-formgroup"><label for="tw-project-path">本机项目目录</label><input id="tw-project-path" name="path" placeholder="/Users/你的用户名/Projects/项目名" required><p class="tw-formhint">使用已存在的绝对路径。Agent 会在这个目录中读取和修改项目文件。</p></div><button type="submit" class="tw-btn tw-primary"${busy ? ' disabled' : ''}>创建项目</button></form></div>`;
}
function workerPage() {
  const running = tasks().filter(t => t.status === 'running' && !t.cancelFailed);
  const pending = tasks().filter(t => t.cancelFailed);
  const external = running.filter(t => t.executionMode === 'external');
  const local = running.filter(t => t.executionMode !== 'external');
  const row = t => `<div class="tw-workerrow"><span class="tw-avatar busy">${icon('bot')}</span><div style="flex:1;min-width:0"><h4>${esc(workerLabel(t))}</h4><div class="tw-subnote">${esc(projectName(t.projectId))} · ${esc(t.title)}</div><div class="tw-subnote">${esc(t.cancelFailed ? '退出待核实' + (t.processId ? ' · PID ' + t.processId : ' · 未记录 PID') : t.desc || '正在推进')}</div></div><button class="tw-btn" data-open="${esc(t.id)}">查看任务</button></div>`;
  return `<div class="tw-detail"><button class="tw-link" data-back="board">← 返回任务看板</button><h3 style="margin-top:20px">worker</h3><p class="tw-small" style="margin-top:5px">本机 Agent 最多同时运行 ${Number(state.maxWorkers) || 3} 个任务；需要你确认时释放执行位置。</p><section class="tw-section" style="margin-top:24px"><h4>协作 worker <span class="tw-count">${new Set(external.map(t => t.worker).filter(Boolean)).size}</span></h4><p class="tw-small">来自当前协作会话；由负责的 worker 汇报真实进展。</p>${external.map(row).join('') || '<div class="tw-empty" style="margin-top:12px">当前没有协作任务正在运行。</div>'}</section><section class="tw-section"><h4>本机 Agent <span class="tw-count">${local.length}/${Number(state.maxWorkers) || 3}</span></h4>${local.map(row).join('') || `<div class="tw-empty" style="margin-top:12px">${pending.length ? '没有正常执行中的任务；下方 Agent 退出状态仍待核实。' : '执行位置空闲，等待已启动且满足依赖的任务。'}</div>`}</section>${pending.length ? `<section class="tw-section"><h4>退出待核实 <span class="tw-count tw-urgent">${pending.length}</span></h4><p class="tw-small">原 Agent 可能仍在写入项目，请先打开对应任务处理。</p>${pending.map(row).join('')}</section>` : ''}</div>`;
}
const capabilityNames = {plan:'规划',execute:'执行',resume:'续接会话',stream:'流式记录',artifacts:'产物',cancel:'取消',readonly:'只读规划','read-only':'只读规划','structured-output':'结构化结果'};
function adapterPage() {
  return `<div class="tw-detail"><div class="tw-detailhead"><button class="tw-link" data-back="board">← 返回任务看板</button><span class="tw-badge">本机检测</span></div><h3>选择你的 Agent</h3><p class="tw-small" style="margin-top:7px">同一套任务、反馈与产物，可以交给不同 Agent 执行。</p>${state.adapters.map(a => `<div class="tw-adapterrow"><span class="tw-avatar">${icon('terminal')}</span><div class="tw-adapterinfo"><h4>${esc(a.name)}</h4><div class="tw-subnote">${esc(a.description || '')}</div><div class="tw-subnote">${arr(a.capabilities).map(c => esc(capabilityNames[c] || c)).join(' · ')}</div>${a.executable ? `<div class="tw-projectpath" style="margin-top:6px">${esc(a.executable)}</div>` : ''}</div><span class="tw-badge ${a.available && a.enabled ? 'done' : 'failed'}">${!a.enabled ? '已停用' : a.available ? '已找到' : '未找到'}</span></div>`).join('')}<section class="tw-section" style="margin-top:24px"><h4>添加其他 Code Agent</h4><p style="font-size:12px">通过本机适配器配置文件接入其他 Agent，重启服务后会在这里显示。新建任务时即可选择。</p><p class="tw-subnote">“已找到”表示检测到可执行程序；实际登录状态会在启动任务时确认。</p></section><section class="tw-section"><h4>从你习惯的入口开始</h4><p style="font-size:12px">网页、命令行和支持 MCP 的 Agent 共用同一套任务记录。</p></section></div>`;
}
function previewPage() {
  const t = currentTask();
  const a = t && artifacts(t)[preview?.index];
  if (!t || !a) return '<div class="tw-loader">找不到这份产物。<button class="tw-link" data-back="detail">返回任务</button></div>';
  return `<div class="tw-detail"><div class="tw-detailhead"><button class="tw-link" data-back="detail">← 返回任务</button><span class="tw-small">产物预览</span></div><h3>${esc(a.name)}</h3><div class="tw-subnote">来自任务：${esc(t.title)}</div>${preview.error ? `<div class="tw-error">${esc(preview.error)}</div><button class="tw-btn" data-artifact="${preview.index}" data-task="${esc(t.id)}">重新加载</button>` : `<div class="tw-preview">${esc(preview.loading ? '正在读取产物…' : preview.body || '文件内容为空。')}</div>`}</div>`;
}
const nodeColors = {blue:'蓝色',violet:'紫色',teal:'青色',amber:'琥珀',rose:'玫瑰',slate:'灰色'};
const outcomes = {exploring:'探索中',adopted:'已采用',discarded:'已放弃',inconclusive:'暂无定论'};
const milestoneTypes = {node_created:'建立节点',node_updated:'调整方向',node_conclusion:'更新结论',task_created:'建立任务',task_status:'任务进展',task_result:'更新结果',task_node_changed:'归入节点',project_deleted:'移至回收站',project_restored:'恢复项目',migration_snapshot:'迁移时快照'};
function draftKey() { return `${page}:${['nodeDetail','nodeEdit','nodeNew'].includes(page) ? selectedNode || '' : selected || ''}`; }
function nodeFor(id) { return state.nodes.find(n => n.id === id); }
function nodeColor(n) { return Object.hasOwn(nodeColors, n?.color) ? n.color : 'slate'; }
function nodesInProject(id = project) { return state.nodes.filter(n => (id === 'all' || n.projectId === id) && projectFor(n.projectId)); }
function textMatches(...values) { return !textFilter || values.some(v => String(v || '').toLocaleLowerCase().includes(textFilter.toLocaleLowerCase())); }
function taskMatchesFilters(t) {
  const n = nodeFor(t.nodeId);
  return (nodeFilter === 'all' || (nodeFilter === 'unassigned' ? !n : n?.id === nodeFilter)) &&
    (colorFilter === 'all' || (n && nodeColor(n) === colorFilter)) &&
    textMatches(t.id,t.title,t.goal,t.desc,n?.title,n?.hypothesis,n?.conclusion);
}
function nodeMatchesFilters(n) {
  return (nodeFilter === 'all' || nodeFilter === n.id) && (colorFilter === 'all' || nodeColor(n) === colorFilter) && textMatches(n.title,n.hypothesis,n.conclusion);
}
function nodeChip(n, interactive = false) {
  const content = `<span class="tw-node-dot" aria-hidden="true"></span><span>${esc(n?.title || '未归类')}</span>`;
  const attrs = `class="tw-node-chip node-${nodeColor(n)}" title="${esc(n ? nodeColors[nodeColor(n)] + ' · ' + n.title : '尚未归入探索节点')}"`;
  return interactive && n ? `<button type="button" ${attrs} data-node="${esc(n.id)}">${content}</button>` : `<span ${attrs}>${content}</span>`;
}
function nodeOptions(projectId, id = '') {
  return `<option value="">未归类</option>${nodesInProject(projectId).map(n => `<option value="${esc(n.id)}"${id === n.id ? ' selected' : ''}>${esc(nodeColors[nodeColor(n)] + ' · ' + n.title)}</option>`).join('')}`;
}
function renderExplorationNavigation() {
  const section = ['nodeDetail','nodeEdit','nodeNew','nodes'].includes(page) ? 'nodes' : ['projects','newProject'].includes(page) ? 'projects' : page === 'trajectory' ? 'trajectory' : 'board';
  $('#tw-sections').innerHTML = `<div class="tw-tabs">${[['board','任务'],['nodes','探索节点'],['trajectory','Trajectory'],['projects','项目管理']].map(([id,label]) => `<button class="tw-tab" data-section="${id}" aria-pressed="${section === id}">${label}${id === 'nodes' ? `<span class="tw-count">${nodesInProject().length}</span>` : ''}</button>`).join('')}</div><span class="tw-small tw-hierarchy">项目 → 探索节点 → 任务 / 子任务</span>`;
  $('#tw-navigation').hidden = page !== 'board';
  const filters = $('#tw-node-filters');
  filters.hidden = !['board','nodes','trajectory'].includes(page) || !state.projects.length;
  if (filters.hidden) return;
  filters.innerHTML = `<div class="tw-filter-field"><label for="tw-node-filter">探索节点</label><select id="tw-node-filter"><option value="all">全部节点</option>${page !== 'nodes' ? '<option value="unassigned">未归类</option>' : ''}${nodesInProject().map(n => `<option value="${esc(n.id)}">${esc((project === 'all' ? projectName(n.projectId) + ' / ' : '') + n.title)}</option>`).join('')}</select></div><div class="tw-filter-field tw-color-filter"><label for="tw-color-filter">节点颜色</label><select id="tw-color-filter"><option value="all">全部颜色</option>${Object.entries(nodeColors).map(([id,label]) => `<option value="${id}">${label}</option>`).join('')}</select></div><div class="tw-filter-field tw-search-filter"><label for="tw-text-filter">搜索</label><input id="tw-text-filter" type="search" placeholder="任务、节点、结论…" value="${esc(textFilter)}" autocomplete="off"></div>${nodeFilter !== 'all' || colorFilter !== 'all' || textFilter ? '<button type="button" class="tw-link" data-clear-filters>清除筛选</button>' : ''}`;
  $('#tw-node-filter').value = nodeFilter;
  $('#tw-color-filter').value = colorFilter;
}
function emptyProjects() {
  return `<div class="tw-zero"><h3>从一个项目开始</h3><p class="tw-small">把一个长期目标放进项目，再记录探索方向、任务和结论。</p><div class="tw-actions" style="justify-content:center;margin-top:18px"><button class="tw-btn tw-primary" data-new-project>＋ 新建项目</button><button class="tw-btn" data-section="projects">查看回收站${state.deletedProjects.length ? ' · ' + state.deletedProjects.length : ''}</button></div></div>`;
}
function projectManagement() {
  const activeCard = p => {
    const ts = tasks().filter(t => t.projectId === p.id), unconfirmed = ts.some(t => t.cancelFailed), blocked = unconfirmed || ts.some(t => t.status === 'running' || t.scheduled);
    return `<article class="tw-project-card"><div><h4>${esc(p.name)}</h4><p class="tw-projectpath">${esc(p.path)}</p><p class="tw-subnote">${nodesInProject(p.id).length} 个探索节点 · ${ts.length} 项任务</p></div><div class="tw-actions"><button class="tw-btn" data-project-open="${esc(p.id)}">打开项目</button><button class="tw-btn tw-danger" data-project-delete="${esc(p.id)}"${busy || blocked ? ' disabled' : ''}>移至回收站</button></div>${blocked ? `<p class="tw-subnote tw-project-blocked">${unconfirmed ? '仍有 Agent 未确认退出。请先在对应任务中确认进程已退出、写入已停止，再删除项目。' : '仍有任务运行或已加入队列，请先结束或取消这些任务。'}</p>` : ''}</article>`;
  };
  return `<div class="tw-detail"><div class="tw-flex tw-between tw-wrap tw-page-heading"><div><h3>项目管理</h3><p class="tw-small">删除后可从回收站恢复；本机项目文件始终保留。</p></div><button class="tw-btn tw-primary" data-new-project>＋ 新建项目</button></div><section class="tw-section"><h4>正在使用 <span class="tw-count">${state.projects.length}</span></h4>${state.projects.map(activeCard).join('') || '<div class="tw-empty">当前没有项目。你可以新建一个，或恢复下方项目。</div>'}</section><section class="tw-section"><h4>回收站 <span class="tw-count">${state.deletedProjects.length}</span></h4>${state.deletedProjects.map(p => `<article class="tw-project-card"><div><h4>${esc(p.name)}</h4><p class="tw-projectpath">${esc(p.path)}</p><p class="tw-subnote">${esc(timeLabel(p.deletedAt))} 移至回收站 · 节点、任务与历史一并保留</p></div><button class="tw-btn" data-project-restore="${esc(p.id)}"${busy ? ' disabled' : ''}>恢复项目</button></article>`).join('') || '<div class="tw-empty">回收站为空。</div>'}</section></div>`;
}
function nodeCard(n) {
  const ts = tasks().filter(t => t.nodeId === n.id);
  return `<button class="tw-node-card node-${nodeColor(n)}" data-node="${esc(n.id)}"><div class="tw-flex tw-between"><span class="tw-small">${esc(projectName(n.projectId))}</span><span class="tw-outcome">${esc(outcomes[n.outcome] || outcomes.exploring)}</span></div>${nodeChip(n)}<p class="tw-node-hypothesis">${esc(n.hypothesis || '尚未记录假设')}</p>${n.conclusion ? `<div class="tw-node-summary"><strong>当前结论</strong><p>${esc(n.conclusion)}</p></div>` : ''}<div class="tw-node-footer"><span>${ts.length} 项任务 · ${ts.filter(t => t.status === 'done').length} 项完成</span><span>${arr(n.parentIds).length > 1 ? '多方向汇合' : arr(n.parentIds).length ? '承接前序探索' : '独立起点'} ↗</span></div></button>`;
}
function nodePage() {
  if (!state.projects.length) return emptyProjects();
  const ns = nodesInProject().filter(nodeMatchesFilters);
  return `<div class="tw-detail"><div class="tw-flex tw-between tw-wrap tw-page-heading"><div><h3>探索节点</h3><p class="tw-small">一个节点记录一个方向或假设。颜色标识方向，任务状态单独记录。</p></div><button class="tw-btn tw-primary" data-new-node>＋ 新建节点</button></div>${ns.length ? `<div class="tw-node-grid">${ns.map(nodeCard).join('')}</div>` : `<div class="tw-zero"><h3>${nodesInProject().length ? '没有符合筛选的节点' : '把第一个探索方向记下来'}</h3><p class="tw-small">${nodesInProject().length ? '可以调整颜色或关键词，查看其他方向。' : '例如一个待验证的假设、一条备选方案，或多个方向汇合后的新想法。已有任务仍保留在“未归类”。'}</p><button class="tw-btn" ${nodesInProject().length ? 'data-clear-filters' : 'data-new-node'}>${nodesInProject().length ? '清除筛选' : '新建第一个节点'}</button></div>`}</div>`;
}
function descendantsOf(id) {
  const found = new Set([id]); let changed = true;
  while (changed) { changed = false; state.nodes.forEach(n => { if (!found.has(n.id) && arr(n.parentIds).some(p => found.has(p))) { found.add(n.id); changed = true; } }); }
  return found;
}
function outcomeOptions(value = 'exploring') { return Object.entries(outcomes).map(([id,label]) => `<option value="${id}"${value === id ? ' selected' : ''}>${label}</option>`).join(''); }
function nodeForm() {
  if (!state.projects.length) return emptyProjects();
  const existing = page === 'nodeEdit' ? nodeFor(selectedNode) : null;
  if (page === 'nodeEdit' && !existing) return '<div class="tw-loader">找不到这个探索节点。</div>';
  const draft = drafts.get(draftKey()) || {};
  const projectId = existing?.projectId || draft['tw-node-project'] || (project === 'all' ? state.projects[0].id : project);
  const excluded = existing ? descendantsOf(existing.id) : new Set();
  const parents = nodesInProject(projectId).filter(n => !excluded.has(n.id));
  const color = existing?.color || Object.keys(nodeColors)[nodesInProject(projectId).length % Object.keys(nodeColors).length];
  return `<div class="tw-detail"><button class="tw-link" ${existing ? `data-node="${esc(existing.id)}"` : 'data-section="nodes"'}>← ${existing ? '返回节点' : '返回探索节点'}</button><form id="tw-node-form" class="tw-newform"><h3 style="margin-bottom:20px">${existing ? '编辑探索节点' : '新建探索节点'}</h3><div class="tw-formgroup"><label for="tw-node-project">所属项目</label><select id="tw-node-project" required${existing ? ' disabled' : ''}>${projectOptions(projectId)}</select></div><div class="tw-formgroup"><label for="tw-node-title">方向 / 假设名称</label><input id="tw-node-title" required maxlength="160" value="${esc(existing?.title || '')}" placeholder="用一句话说明这次要探索什么"></div><div class="tw-formgroup"><label for="tw-node-hypothesis">假设与背景</label><textarea id="tw-node-hypothesis" maxlength="20000" placeholder="为什么值得尝试？预期观察到什么？">${esc(existing?.hypothesis || '')}</textarea></div><div class="tw-formgroup"><label for="tw-node-color">节点颜色</label><select id="tw-node-color">${Object.entries(nodeColors).map(([id,label]) => `<option value="${id}"${id === color ? ' selected' : ''}>${label}</option>`).join('')}</select><p class="tw-formhint">同一节点的任务和关系图沿用这个颜色。</p></div><fieldset class="tw-parent-field"><legend>承接哪些前序节点？（可多选）</legend><p class="tw-formhint">不选是独立起点；选择多个表示将几个方向汇合。关系不会自动启动任务。</p>${parents.map(n => `<label class="tw-parent-choice node-${nodeColor(n)}"><input id="tw-parent-${esc(n.id)}" type="checkbox" data-parent-id="${esc(n.id)}"${arr(existing?.parentIds).includes(n.id) ? ' checked' : ''}><span class="tw-node-dot"></span><span>${esc(n.title)}</span></label>`).join('') || '<p class="tw-small" style="margin-top:12px">这个项目还没有可承接的其他节点。</p>'}</fieldset><div class="tw-formgroup"><label for="tw-node-outcome">探索结果</label><select id="tw-node-outcome">${outcomeOptions(existing?.outcome)}</select></div><div class="tw-formgroup"><label for="tw-node-conclusion">当前结论</label><textarea id="tw-node-conclusion" maxlength="40000" placeholder="观察到了什么，得到什么结论，下一步如何选择？">${esc(existing?.conclusion || '')}</textarea><p class="tw-formhint">采用、放弃或暂无定论时，需要写下原因。每次修改都会留下历史版本。</p></div><button type="submit" class="tw-btn tw-primary"${busy ? ' disabled' : ''}>${existing ? '保存修改' : '创建节点'}</button></form></div>`;
}
function familyFor(t) {
  let parent = t; const seen = new Set();
  while (parent.parentTaskId && !seen.has(parent.id)) { seen.add(parent.id); const next = taskFor(parent.parentTaskId); if (!next || next.projectId !== t.projectId) break; parent = next; }
  const family = new Set([parent.id]); let changed = true;
  while (changed) { changed = false; tasks().filter(x => x.projectId === t.projectId).forEach(x => { if (!family.has(x.id) && family.has(x.parentTaskId)) { family.add(x.id); changed = true; } }); }
  return tasks().filter(x => family.has(x.id));
}
function taskNodeSection(t) {
  const n = nodeFor(t.nodeId), family = familyFor(t), unconfirmed = family.some(x => x.cancelFailed), locked = unconfirmed || family.some(x => x.status === 'running' || x.scheduled);
  return `<section class="tw-section tw-task-node-section"><div class="tw-flex tw-between tw-wrap"><h4>所属探索节点</h4>${nodeChip(n, true)}</div><form id="tw-task-node-form" class="tw-node-assignment"><label class="tw-small" for="tw-assigned-node">${family.length > 1 ? `为这个计划及全部 ${family.length} 项任务选择节点` : '将任务归入一个探索方向'}</label><div class="tw-flex"><select id="tw-assigned-node"${locked ? ' disabled' : ''}>${nodeOptions(t.projectId, t.nodeId)}</select><button type="submit" class="tw-btn"${busy || locked ? ' disabled' : ''}>保存归属</button></div><p class="tw-formhint">${unconfirmed ? '任务家族中仍有 Agent 未确认退出。请先在对应任务中确认退出并停止写入，再调整归属。' : locked ? '任务家族中仍有运行或排队的任务，结束或取消后可调整归属。' : '计划与子任务始终属于同一节点。'}</p></form></section>`;
}
function nodeTaskTree(ts) {
  const byId = new Map(ts.map(t => [t.id,t])), seen = new Set();
  const renderTask = (t, depth = 0) => {
    if (seen.has(t.id)) return '';
    seen.add(t.id);
    const children = ts.filter(child => child.parentTaskId === t.id);
    const inset = Math.min(depth, 6) * 14;
    return `<button class="tw-artifact" data-open="${esc(t.id)}" style="margin-left:${inset}px;width:calc(100% - ${inset}px)"><span>${depth ? '<span class="tw-small">↳ 子任务 · </span>' : children.length ? '<span class="tw-small">计划 · </span>' : ''}${esc(t.title)}</span>${badge(t)}</button>${children.map(child => renderTask(child,depth+1)).join('')}`;
  };
  return ts.filter(t => !byId.has(t.parentTaskId)).map(t => renderTask(t)).join('') + ts.filter(t => !seen.has(t.id)).map(t => renderTask(t)).join('');
}
function nodeDetail() {
  const n = nodeFor(selectedNode);
  if (!n) return '<div class="tw-loader">找不到这个探索节点。<button class="tw-link" data-section="nodes">返回节点列表</button></div>';
  const ts = tasks().filter(t => t.nodeId === n.id);
  const parents = arr(n.parentIds).map(nodeFor).filter(Boolean), children = state.nodes.filter(x => arr(x.parentIds).includes(n.id));
  const history = state.trajectory.filter(e => e.nodeId === n.id && ['node_created','node_updated','node_conclusion'].includes(e.type));
  return `<div class="tw-detail"><div class="tw-detailhead"><button class="tw-link" data-section="nodes">← 返回探索节点</button><span class="tw-small">${esc(projectName(n.projectId))}</span></div><div class="tw-flex tw-between tw-wrap"><h3 class="tw-node-heading node-${nodeColor(n)}"><span class="tw-node-dot"></span>${esc(n.title)}</h3><div class="tw-actions"><span class="tw-outcome">${esc(outcomes[n.outcome] || outcomes.exploring)}</span><button class="tw-btn" data-edit-node="${esc(n.id)}">编辑节点</button><button class="tw-btn tw-primary" data-new-task-node="${esc(n.id)}">＋ 新建任务</button></div></div><div class="tw-detailgrid"><div><section class="tw-section"><h4>假设与背景</h4><p class="tw-prose">${esc(n.hypothesis || '尚未记录假设。')}</p></section><section class="tw-section"><h4>探索关系</h4><p class="tw-small">前序方向${parents.length > 1 ? ' · 这个节点汇合了多个方向' : ''}</p><div class="tw-node-links">${parents.map(p => nodeChip(p,true)).join('') || '<span class="tw-small">独立起点</span>'}</div><p class="tw-small" style="margin-top:14px">后续分支</p><div class="tw-node-links">${children.map(c => nodeChip(c,true)).join('') || '<span class="tw-small">尚无后续分支</span>'}</div><button class="tw-link" data-node-trajectory="${esc(n.id)}" style="margin-top:14px">在 Trajectory 中查看 →</button></section><section class="tw-section"><div class="tw-flex tw-between tw-wrap"><h4>任务与子任务 <span class="tw-count">${ts.length}</span></h4><button class="tw-link" data-node-tasks="${esc(n.id)}">在任务看板中查看 →</button></div>${ts.length ? `<div class="tw-node-task-list">${nodeTaskTree(ts)}</div>` : '<div class="tw-empty">还没有任务。新建任务或将现有任务归入这个节点。</div>'}</section><form id="tw-conclusion-form" class="tw-section tw-conclusion-editor"><h4>长期结论</h4><div class="tw-formgroup"><label for="tw-conclusion-outcome">当前探索结果</label><select id="tw-conclusion-outcome">${outcomeOptions(n.outcome)}</select></div><div class="tw-formgroup"><label for="tw-conclusion-text">结论与依据</label><textarea id="tw-conclusion-text" maxlength="40000" placeholder="保留判断、证据与放弃或继续的原因。">${esc(n.conclusion || '')}</textarea></div><button type="submit" class="tw-btn tw-primary"${busy ? ' disabled' : ''}>保存结论</button><p class="tw-formhint">新结论会追加到历史，之前的判断仍可回看。</p></form></div><aside><section class="tw-section"><h4>当前结论</h4><p class="tw-prose tw-saved-conclusion">${esc(n.conclusion || '尚未形成结论。')}</p><p class="tw-subnote">更新于 ${esc(timeLabel(n.updatedAt))}</p></section><section class="tw-section"><h4>节点历史</h4>${milestoneList(history, true)}</section></aside></div></div>`;
}
function syncNodeFormConstraints() {
  const outcome = $('#tw-node-outcome'), conclusion = $('#tw-node-conclusion');
  if (outcome && conclusion) conclusion.required = outcome.value !== 'exploring';
  const result = $('#tw-conclusion-outcome'), text = $('#tw-conclusion-text');
  if (result && text) text.required = result.value !== 'exploring';
}
function graphFor(projectId) {
  const ns = nodesInProject(projectId).slice().sort((a,b) => String(a.createdAt).localeCompare(String(b.createdAt)) || a.id.localeCompare(b.id));
  if (!ns.length) return '';
  const byId = new Map(ns.map(n => [n.id,n])), levels = new Map(), visiting = new Set();
  function depth(n) {
    if (levels.has(n.id)) return levels.get(n.id);
    if (visiting.has(n.id)) return 0;
    visiting.add(n.id);
    const parents = arr(n.parentIds).map(id => byId.get(id)).filter(Boolean);
    const value = parents.length ? Math.max(...parents.map(p => depth(p))) + 1 : 0;
    visiting.delete(n.id); levels.set(n.id,value); return value;
  }
  ns.forEach(depth);
  const grouped = [];
  ns.forEach(n => { const level = levels.get(n.id); (grouped[level] ||= []).push(n); });
  const positions = new Map();
  const cardWidth = 214, cardHeight = 106, gapX = 64, gapY = 38, margin = 26;
  const rows = Math.max(...grouped.map(column => column.length));
  const width = margin * 2 + grouped.length * cardWidth + (grouped.length - 1) * gapX;
  const height = margin * 2 + rows * cardHeight + (rows - 1) * gapY;
  grouped.forEach((column,level) => column.forEach((n,row) => positions.set(n.id,{x:margin + level*(cardWidth+gapX),y:margin + row*(cardHeight+gapY) + (rows-column.length)*(cardHeight+gapY)/2})));
  const paths = ns.flatMap(n => arr(n.parentIds).filter(id => positions.has(id)).map(id => {
    const p = positions.get(id), c = positions.get(n.id), x1 = p.x+cardWidth, y1=p.y+cardHeight/2, x2=c.x, y2=c.y+cardHeight/2, mid=(x1+x2)/2;
    return `<path class="node-${nodeColor(n)}" d="M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2-7} ${y2}"/><path class="tw-arrowhead node-${nodeColor(n)}" d="M ${x2-9} ${y2-4} L ${x2-3} ${y2} L ${x2-9} ${y2+4}"/>`;
  })).join('');
  return `<section class="tw-graph-project"><div class="tw-flex tw-between tw-wrap"><h4>${esc(projectName(projectId))}</h4><span class="tw-small">${ns.length} 个节点 · 从左向右承接</span></div><div class="tw-graph-scroll" data-project="${esc(projectId)}" tabindex="0" aria-label="${esc(projectName(projectId))} 的探索关系，可横向滚动"><div class="tw-graph-canvas" style="width:${width}px;height:${height}px"><svg class="tw-graph-edges" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" aria-hidden="true">${paths}</svg>${ns.map(n => {
    const p=positions.get(n.id), ts=tasks().filter(t=>t.nodeId===n.id), dim=!nodeMatchesFilters(n);
    return `<button class="tw-graph-node node-${nodeColor(n)}${dim ? ' tw-graph-dim' : ''}" style="left:${p.x}px;top:${p.y}px;width:${cardWidth}px;height:${cardHeight}px" data-node="${esc(n.id)}" title="${esc(n.title)}" aria-label="探索节点：${esc(n.title)}，${esc(outcomes[n.outcome] || outcomes.exploring)}"><span class="tw-flex tw-between"><span class="tw-node-dot"></span><span class="tw-small">${esc(outcomes[n.outcome] || outcomes.exploring)}</span></span><strong>${esc(n.title)}</strong><span class="tw-small">${ts.length} 项任务${arr(n.parentIds).length>1 ? ' · 汇合 '+n.parentIds.length+' 个方向' : ''}</span></button>`;
  }).join('')}</div></div></section>`;
}
function milestoneList(events, compact = false) {
  if (!events.length) return '<div class="tw-empty">这里还没有历史记录。</div>';
  const ordered = events.slice().sort((a,b) => String(a.at).localeCompare(String(b.at)));
  const groups = new Map();
  ordered.forEach(e => {
    const d = new Date(e.at), valid = !Number.isNaN(d.getTime());
    const month = valid ? `${d.getFullYear()} 年 ${String(d.getMonth()+1).padStart(2,'0')} 月` : '未注明日期';
    if (!groups.has(month)) groups.set(month,[]); groups.get(month).push(e);
  });
  return `<div class="tw-timeline${compact ? ' compact' : ''}">${Array.from(groups).map(([month,items])=>`<section class="tw-timeline-month"><h4>${month}</h4><ol>${items.map(e => {
    const n=nodeFor(e.nodeId), d=new Date(e.at), valid=!Number.isNaN(d.getTime()), summary=String(e.summary || '');
    const date = valid ? `${String(d.getDate()).padStart(2,'0')} 日 · ${d.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false})}` : String(e.at || '时间未记录');
    const conclusion = e.type === 'node_conclusion';
    return `<li class="tw-milestone node-${nodeColor(n)}${conclusion ? ' tw-conclusion-milestone' : ''}"><span class="tw-timeline-dot"></span><div class="tw-milestone-meta"><time datetime="${esc(e.at)}">${esc(date)}</time><span>${esc(milestoneTypes[e.type] || '项目记录')}</span>${project === 'all' && !compact ? `<span>${esc(projectName(e.projectId))}</span>` : ''}</div><h4>${esc(e.title || milestoneTypes[e.type] || '项目记录')}</h4>${(summary.length > 500 || e.type === 'migration_snapshot') ? `<details class="tw-history-detail" data-history-id="${esc(e.id)}"><summary>${esc(summary.slice(0,220))}… <span class="tw-link">展开完整记录</span></summary><p class="tw-prose">${esc(summary)}</p></details>` : summary ? `<p class="tw-prose">${esc(summary)}</p>` : ''}<div class="tw-milestone-links">${n ? nodeChip(n,true) : ''}${e.taskId && taskFor(e.taskId) ? `<button class="tw-link" data-open="${esc(e.taskId)}">${esc(e.taskId)} · 查看任务 →</button>` : ''}</div></li>`;
  }).join('')}</ol></section>`).join('')}</div>`;
}
let conclusionsOnly = false, showMigrationSnapshots = false;
function trajectoryPage() {
  if (!state.projects.length) return emptyProjects();
  const events = state.trajectory.filter(e => {
    const n=nodeFor(e.nodeId);
    return projectFor(e.projectId) && (project === 'all' || e.projectId === project) &&
      (nodeFilter === 'all' || (nodeFilter === 'unassigned' ? !e.nodeId : e.nodeId === nodeFilter)) &&
      (colorFilter === 'all' || (n && nodeColor(n) === colorFilter)) &&
      (!conclusionsOnly || e.type === 'node_conclusion') && (showMigrationSnapshots || e.type !== 'migration_snapshot') && textMatches(e.title,e.summary,n?.title,n?.hypothesis,n?.conclusion);
  });
  const projectsWithNodes = state.projects.filter(p => (project === 'all' || p.id === project) && nodesInProject(p.id).some(nodeMatchesFilters));
  return `<div class="tw-detail"><div class="tw-flex tw-between tw-wrap tw-page-heading"><div><h3>Trajectory · 探索轨迹</h3><p class="tw-small">看方向如何分叉、并行与汇合，也保留每次选择的依据。</p></div><button class="tw-btn" data-new-node>＋ 新建节点</button></div><section class="tw-section"><h4>探索关系</h4><p class="tw-small">箭头表示方向的承接；上下分支可以并行。真实发生时间记录在下方。</p>${projectsWithNodes.map(p=>graphFor(p.id)).join('') || `<div class="tw-empty" style="margin-top:16px">${nodesInProject().length ? '当前筛选没有探索节点；未归类任务仍可在下方时间线查看。' : '建立探索节点后，这里会出现分支关系。已有任务历史保留在下方。'}</div>`}</section><section class="tw-section"><div class="tw-flex tw-between tw-wrap tw-page-heading"><div><h4>时间线 <span class="tw-count">${events.length}</span></h4><p class="tw-small">按真实记录时间排列 · 历史结论与结果会保留</p></div><div><label class="tw-checkline"><input id="tw-conclusions-only" type="checkbox"${conclusionsOnly ? ' checked' : ''}>只看结论修订</label><label class="tw-checkline"><input id="tw-show-migrations" type="checkbox"${showMigrationSnapshots ? ' checked' : ''}>显示初始迁移快照</label></div></div>${milestoneList(events)}</section></div>`;
}
function openNode(id, edit = false) {
  captureForm(); selectedNode = id; selected = null; page = edit ? 'nodeEdit' : 'nodeDetail'; setNotice(''); render(); window.scrollTo({top:0,behavior:'instant'});
}
function openNewTask(nodeId = null) {
  captureForm();
  if (!state.projects.length) { page='newProject';selected=null;setNotice('');render();return; }
  const n = nodeFor(nodeId);
  if (n) { project=n.projectId; drafts.set('new:',{...(drafts.get('new:') || {}),'tw-task-project':n.projectId,'tw-task-node':n.id,'tw-deps':[]}); }
  page='new';selected=null;setNotice('');render();$('#tw-title')?.focus();
}
function handleExplorationClick(button,d) {
  if (d.section) { captureForm();page=d.section;selected=null;selectedNode=null; if(page==='nodes'&&nodeFilter==='unassigned')nodeFilter='all';setNotice('');saveView();render();return true; }
  if ('clearFilters' in d) { nodeFilter='all';colorFilter='all';textFilter='';saveView();render();return true; }
  if ('newProject' in d) { navigate('newProject',null);return true; }
  if (d.projectOpen) { captureForm();project=d.projectOpen;nodeFilter='all';colorFilter='all';textFilter='';page='nodes';selected=null;selectedNode=null;saveView();setNotice('');render();return true; }
  if (d.projectDelete || d.projectRestore) {
    const id=d.projectDelete || d.projectRestore, action=d.projectDelete?'delete':'restore';
    mutate(async()=>{await api(`/api/projects/${encodeURIComponent(id)}/actions`,{method:'POST',body:JSON.stringify({action})});setNotice(action==='delete'?'项目已移至回收站，任务和历史均已保留。':'项目已恢复，节点、任务和历史已找回。');});return true;
  }
  if (d.node) { openNode(d.node);return true; }
  if (d.editNode) { openNode(d.editNode,true);return true; }
  if ('newNode' in d) { captureForm();selectedNode=null;selected=null;page=state.projects.length?'nodeNew':'newProject';setNotice('');render();$('#tw-node-title')?.focus();return true; }
  if (d.newTaskNode) { openNewTask(d.newTaskNode);return true; }
  if (d.nodeTasks || d.nodeTrajectory) {
    const n=nodeFor(d.nodeTasks || d.nodeTrajectory); if(!n)return true;
    captureForm();project=n.projectId;nodeFilter=n.id;colorFilter='all';textFilter='';filter='all';page=d.nodeTasks?'board':'trajectory';selected=null;selectedNode=null;setNotice('');saveView();render();return true;
  }
  return false;
}
function handleExplorationChange(el) {
  if (el.id==='tw-node-filter' || el.id==='tw-color-filter') { if(el.id==='tw-node-filter')nodeFilter=el.value;else colorFilter=el.value;saveView();render();return true; }
  if (el.id==='tw-node-project') {
    captureForm();const draft=drafts.get(draftKey());if(draft)Object.keys(draft).filter(k=>k.startsWith('tw-parent-')).forEach(k=>delete draft[k]);render();return true;
  }
  if (el.id==='tw-node-outcome' || el.id==='tw-conclusion-outcome') {syncNodeFormConstraints();return true;}
  if (el.id==='tw-show-migrations') {showMigrationSnapshots=el.checked;render();return true;}
  if (el.id==='tw-conclusions-only') {conclusionsOnly=el.checked;render();return true;}
  return false;
}
function submitExplorationForm(form) {
  if (form.id==='tw-node-form') {
    const editing=page==='nodeEdit', id=selectedNode, key=draftKey();
    const data={title:$('#tw-node-title').value.trim(),hypothesis:$('#tw-node-hypothesis').value.trim(),color:$('#tw-node-color').value,parentIds:Array.from(form.querySelectorAll('[data-parent-id]:checked')).map(el=>el.dataset.parentId),outcome:$('#tw-node-outcome').value,conclusion:$('#tw-node-conclusion').value.trim()};
    if(!editing)data.projectId=$('#tw-node-project').value;
    if(!data.title || (data.outcome!=='exploring'&&!data.conclusion)){setNotice('请填写节点名称，并为当前探索结果补充结论。',true);return true;}
    mutate(async()=>{const node=await api(editing?`/api/nodes/${encodeURIComponent(id)}`:'/api/nodes',{method:'POST',body:JSON.stringify(data)});drafts.delete(key);drafts.delete(`nodeDetail:${node.id}`);renderedKey=null;selectedNode=node.id;selected=null;project=node.projectId;page='nodeDetail';setNotice(editing?'节点已更新，修改记录已保留。':'探索节点已创建，可以开始分配任务。');});return true;
  }
  if(form.id==='tw-conclusion-form') {
    const id=selectedNode,key=draftKey(),data={outcome:$('#tw-conclusion-outcome').value,conclusion:$('#tw-conclusion-text').value.trim()};
    if(data.outcome!=='exploring'&&!data.conclusion){setNotice('请写下采用、放弃或暂无定论的原因。',true);return true;}
    mutate(async()=>{await api(`/api/nodes/${encodeURIComponent(id)}`,{method:'POST',body:JSON.stringify(data)});drafts.delete(key);drafts.delete(`nodeEdit:${id}`);renderedKey=null;setNotice('结论已保存；之前的版本保留在节点历史与 Trajectory 中。');});return true;
  }
  if(form.id==='tw-task-node-form') {
    const id=selected,nodeId=$('#tw-assigned-node').value || null;
    mutate(async()=>{await api(`/api/tasks/${encodeURIComponent(id)}/node`,{method:'POST',body:JSON.stringify({nodeId})});const draft=drafts.get(`detail:${id}`);if(draft)draft['tw-assigned-node']=nodeId || '';setNotice('节点归属已更新，计划和子任务已同步。');});return true;
  }
  return false;
}
root.addEventListener('input',event=>{if(event.target.id==='tw-text-filter'){textFilter=event.target.value;if(!event.isComposing&&!composingText)render();}});
root.addEventListener('compositionstart',()=>{composingText=true;});
root.addEventListener('compositionend',event=>{composingText=false;if(event.target.id==='tw-text-filter')textFilter=event.target.value;if(renderAfterComposition||event.target.id==='tw-text-filter'){renderAfterComposition=false;render();}});

function render() {
  if (!loaded) return;
  if (composingText) { renderAfterComposition = true; return; }
  const snapshot = captureForm();
  header();
  const pages = {board,detail,new:newTaskForm,newProject:newProjectForm,workers:workerPage,adapters:adapterPage,preview:previewPage,nodes:nodePage,nodeDetail,nodeEdit:nodeForm,nodeNew:nodeForm,trajectory:trajectoryPage,projects:projectManagement};
  $('#tw-content').innerHTML = (pages[page] || board)();
  renderedKey = draftKey();
  setNotice(notice, noticeError);
  restoreForm(snapshot);
  syncNodeFormConstraints();
}
async function mutate(operation) {
  if (busy) return;
  busy = true; captureForm(); render();
  try {
    await operation();
    await refresh(true);
  } catch (error) {
    setNotice(error.message, true);
  } finally { busy = false; render(); }
}
async function taskAction(action, payload = {}) {
  const id = selected;
  if (!id) return;
  if (currentTask()?.cancelFailed && action !== 'confirm_stopped') { setNotice('请先确认原 Agent 已退出、项目写入已停止。', true); return; }
  await mutate(async () => {
    await api(`/api/tasks/${encodeURIComponent(id)}/actions`, {method:'POST',body:JSON.stringify({action,...payload})});
    const messages = {start:'任务已加入队列，进展会自动更新。',retry:'已提交重试，历史记录保留。',approve_plan:'计划已批准，子任务将按依赖开始执行。',accept:'已验收通过，后续依赖任务可以继续。',feedback:'反馈已保存到任务记录。',cancel:'已提交取消请求，进程停止后会更新状态。',confirm_stopped:'已记录 Agent 退出确认，现在可以重试或调整任务归属。'};
    setNotice(messages[action] || '操作已完成。');
    if (action === 'feedback') {
      drafts.delete(`detail:${id}`);
      const field = $('#tw-feedback'); if (field) field.value = '';
    }
    if (action === 'approve_plan') { page = 'board'; selected = null; filter = 'all'; }
  });
}
async function loadArtifact(taskId, index) {
  captureForm(); selected = taskId; page = 'preview';
  const target = {taskId,index,loading:true,body:'',error:''};
  preview = target; render();
  try {
    const a = await api(`/api/tasks/${encodeURIComponent(taskId)}/artifacts/${index}`);
    target.body = String(a.body ?? '');
  } catch (error) { target.error = error.message; }
  finally { target.loading = false; if (preview === target && page === 'preview') render(); }
}
root.addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button || button.disabled) return;
  const d = button.dataset;
  if (button.id === 'tw-refresh' || 'refresh' in d) { refresh(true); return; }
  if (handleExplorationClick(button, d)) return;
  if (d.open) { navigate('detail', d.open); return; }
  if (d.filter) { captureForm(); filter = d.filter; page = 'board'; selected = null; setNotice(''); saveView(); render(); return; }
  if (d.layout) { captureForm(); view = d.layout; page = 'board'; selected = null; saveView(); render(); return; }
  if (d.back) { navigate(d.back, d.back === 'board' ? null : selected); return; }
  if ('artifact' in d) { loadArtifact(d.task || selected, Number(d.artifact)); return; }
  if (button.id === 'tw-new' || button.id === 'tw-first-task') { openNewTask(); return; }
  if (button.id === 'tw-adapters') { navigate('adapters', null); return; }
  if (button.id === 'tw-workers') { navigate('workers', null); return; }
  if ('focusFeedback' in d) { $('#tw-feedback')?.focus(); $('#tw-feedback')?.scrollIntoView({block:'center',behavior:'smooth'}); return; }
  if (d.action) taskAction(d.action, d.action === 'confirm_stopped' ? {confirmStopped:true} : {});
});
root.addEventListener('change', event => {
  if (handleExplorationChange(event.target)) return;
  if (event.target.id === 'tw-project-filter') {
    captureForm();
    if (event.target.value === '__new__') { page = 'newProject'; selected = null; setNotice(''); render(); $('#tw-project-name')?.focus(); return; }
    project = event.target.value; page = ['nodes','trajectory'].includes(page) ? page : 'board'; selected = null; selectedNode = null; filter = 'all'; nodeFilter = 'all'; colorFilter = 'all'; textFilter = ''; setNotice(''); saveView(); render();
  } else if (event.target.id === 'tw-task-project') {
    captureForm();
    const draft = drafts.get('new:');
    if (draft) { draft['tw-deps'] = []; draft['tw-task-node'] = ''; }
    render();
  }
});
root.addEventListener('submit', event => {
  event.preventDefault();
  if (busy) return;
  const form = event.target;
  if (!form.reportValidity()) return;
  if (submitExplorationForm(form)) return;
  if (form.id === 'tw-feedback-form') {
    const message = $('#tw-feedback').value.trim();
    if (message) taskAction('feedback', {message});
    return;
  }
  if (form.id === 'tw-new-form') {
    const start = event.submitter?.value !== 'save';
    const engine = $('#tw-engine').value || state.adapters[0]?.id || 'codex';
    const data = {
      projectId:$('#tw-task-project').value,title:$('#tw-title').value.trim(),goal:$('#tw-goal').value.trim(),
      criteria:$('#tw-criteria').value.split('\n').map(s => s.trim()).filter(Boolean),engine,
      phase:$('#tw-phase').value,kind:$('#tw-phase').value === 'plan' ? 'plan' : 'result',status:'queued',
      executionMode:'local',scheduled:false,start,nodeId:$('#tw-task-node').value || null,
      deps:Array.from($('#tw-deps')?.selectedOptions || []).map(o => o.value),
    };
    if (!data.title || !data.goal || !data.criteria.length) { setNotice('请填写任务名称、目标和验收标准。', true); return; }
    if (start && (!adapterFor(engine)?.available || !adapterFor(engine)?.enabled)) { setNotice('请选择可用的 Agent，或先保存任务。', true); return; }
    mutate(async () => {
      const task = await api('/api/tasks', {method:'POST',body:JSON.stringify(data)});
      drafts.delete('new:'); renderedKey = null;
      // Leave the form before rerendering so its cleared draft is not recaptured.
      page = 'detail'; selected = task.id; project = task.projectId; filter = 'all'; nodeFilter = task.nodeId || 'all'; colorFilter = 'all'; textFilter = ''; saveView();
      setNotice(start ? '任务已创建并启动。' : '任务已保存，准备好后可从详情页启动。');
    });
    return;
  }
  if (form.id === 'tw-project-form') {
    const data = {name:$('#tw-project-name').value.trim(),path:$('#tw-project-path').value.trim()};
    if (!data.name || !data.path) { setNotice('请填写项目名称和本机目录。', true); return; }
    mutate(async () => {
      const created = await api('/api/projects', {method:'POST',body:JSON.stringify(data)});
      drafts.delete('newProject:'); renderedKey = null; project = created.id; page = 'board'; selected = null; filter = 'all'; saveView();
      setNotice(`项目「${created.name}」已创建。`);
    });
  }
});
window.addEventListener('beforeunload', saveView);
async function poll() { await refresh(); window.setTimeout(poll, 2000); }
poll();
})();
