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
let state = {projects:[], tasks:[], adapters:[], workers:[], maxWorkers:3, version:null};
let page = 'board', selected = null, project = 'all', filter = 'all', view = 'board';
let loaded = false, connected = false, busy = false, notice = '', noticeError = false;
let fetchSequence = 0, preview = null, renderedKey = null;
try {
  const saved = JSON.parse(localStorage.getItem('taskboard.view') || '{}');
  project = typeof saved.project === 'string' ? saved.project : 'all';
  filter = ['all','attention','artifacts'].includes(saved.filter) ? saved.filter : 'all';
  view = saved.view === 'list' ? 'list' : 'board';
} catch (_) { /* Browser storage is optional. */ }
const tasks = () => state.tasks;
const taskFor = id => tasks().find(t => t.id === id);
const currentTask = () => taskFor(selected);
const projectFor = id => state.projects.find(p => p.id === id);
const projectName = id => projectFor(id)?.name || '未归类';
const adapterFor = id => state.adapters.find(a => a.id === id);
const agentName = id => adapterFor(id)?.name || id || '未选择';
const arr = value => Array.isArray(value) ? value : value ? [String(value)] : [];
const artifacts = t => arr(t.artifacts);
const steps = t => arr(t.steps);
const attention = t => ['review','failed'].includes(t.status);
const visibleTasks = () => project === 'all' ? tasks() : tasks().filter(t => t.projectId === project);
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
const badge = t => `<span class="tw-badge ${esc(statusOrder.includes(t.status) ? t.status : '')}">${esc(t.cancelRequested ? '正在停止' : t.status === 'review' ? (t.kind === 'plan' ? '待确认计划' : t.kind === 'question' ? questionAttention(t).badge : '待验收') : statuses[t.status] || t.status)}</span>`;
const projectOptions = id => state.projects.map(p => `<option value="${esc(p.id)}"${p.id === id ? ' selected' : ''}>${esc(p.name)}</option>`).join('');
const agentOptions = id => state.adapters.map(a => `<option value="${esc(a.id)}"${a.id === id ? ' selected' : ''}${!a.enabled || !a.available ? ' disabled' : ''}>${esc(a.name)}${!a.enabled ? '（已停用）' : !a.available ? '（未找到）' : ''}</option>`).join('');
const criteriaHtml = criteria => arr(criteria).length ? `<ul class="tw-criteria">${arr(criteria).map(c => `<li>${esc(c)}</li>`).join('')}</ul>` : '<p class="tw-small">尚未填写验收标准。</p>';
const actionButton = (action, label, primary = false) => `<button type="button" class="tw-btn${primary ? ' tw-primary' : ''}${action === 'cancel' ? ' tw-danger' : ''}" data-action="${action}"${busy ? ' disabled' : ''}>${label}</button>`;
function saveView() {
  try { localStorage.setItem('taskboard.view', JSON.stringify({project, filter, view})); } catch (_) {}
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
    if (project !== 'all' && !projectFor(project)) project = state.projects[0]?.id || 'all';
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
  };
}
function restoreForm(snapshot) {
  const values = drafts.get(`${page}:${selected || ''}`) || {};
  Object.entries(values).forEach(([id, value]) => {
    const el = document.getElementById(id);
    if (!el || !root.contains(el)) return;
    if (el.multiple) Array.from(el.options).forEach(o => { o.selected = arr(value).includes(o.value); });
    else if (el.type === 'checkbox') el.checked = value;
    else if (el.tagName !== 'SELECT' || Array.from(el.options).some(o => o.value === value && !o.disabled)) el.value = value;
  });
  if (snapshot?.key !== `${page}:${selected || ''}`) return;
  const el = snapshot.id ? document.getElementById(snapshot.id) : null;
  if (el && root.contains(el)) {
    el.focus({preventScroll:true});
    if (typeof snapshot.start === 'number' && ['INPUT','TEXTAREA'].includes(el.tagName)) {
      try { el.setSelectionRange(snapshot.start, snapshot.end); } catch (_) {}
    }
  }
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
  $('#tw-workers').innerHTML = `<span class="tw-flex" style="gap:3px">${Array.from({length:Math.min(Math.max(count, 1), 3)}, (_, i) => `<span class="tw-avatar ${count > i ? 'busy' : ''}">${i + 1}</span>`).join('')}</span><span>${count} 位执行者工作中</span>`;
  $('#tw-navigation').innerHTML = `<div class="tw-tabs"><button class="tw-tab" data-filter="all" aria-pressed="${filter === 'all'}">全部任务 <span class="tw-count">${visible.length}</span></button><button class="tw-tab" data-filter="attention" aria-pressed="${filter === 'attention'}">待我处理 <span class="tw-count tw-urgent">${visible.filter(attention).length}</span></button><button class="tw-tab" data-filter="artifacts" aria-pressed="${filter === 'artifacts'}">产物 <span class="tw-count">${visible.reduce((n, t) => n + artifacts(t).length, 0)}</span></button></div><div class="tw-switch" aria-label="任务显示方式"><button data-layout="board" aria-pressed="${view === 'board'}">${icon('columns')}看板</button><button data-layout="list" aria-pressed="${view === 'list'}">${icon('list')}清单</button></div>`;
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
  const next = questionAttention(t);
  const firstLine = next?.description.split('\n').find(line => line.trim())?.trim() || '';
  const summary = firstLine.length > 120 ? firstLine.slice(0, 120) + '…' : firstLine;
  const nextStep = next ? `<strong>下一步：${esc(next.title)}</strong>${summary && summary !== next.title ? `<div style="margin-top:5px">${esc(summary)}</div>` : ''}` : esc(t.desc || t.goal || '');
  const count = Math.max(0, Math.min(Number(t.done) || 0, steps(t).length));
  return `<button type="button" class="tw-task" data-open="${esc(t.id)}" aria-label="查看任务：${esc(t.title)}"><div class="tw-project"><span>${esc(projectName(t.projectId))}</span><span>${esc(t.id)}</span></div><div>${badge(t)}<div class="tw-tasktitle">${esc(t.title)}</div></div><div><div class="tw-engine">${icon('terminal')}${esc(t.executionMode === 'external' ? '协作 worker' : agentName(t.engine))}</div><div class="tw-taskdesc">${nextStep}</div>${steps(t).length ? `<div class="tw-stepbar" aria-label="已完成 ${count} 项，共 ${steps(t).length} 项">${steps(t).map((_, i) => `<span class="${i < count ? 'filled' : ''}"></span>`).join('')}</div><div class="tw-stepcaption">${count}/${steps(t).length} 项已确认完成</div>` : ''}</div><div class="tw-cardfoot"><span class="tw-flex" style="gap:4px">${icon(t.status === 'queued' ? 'link' : 'bot')}${esc(t.status === 'queued' ? queueLabel(t) : t.worker || (t.status === 'done' ? '已验收' : '查看进展'))}</span><span>${artifacts(t).length ? artifacts(t).length + ' 份产物' : next ? '查看操作 →' : '查看 →'}</span></div></button>`;
}
function board() {
  const visible = visibleTasks();
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
  if (t.status === 'review' && t.kind === 'plan') {
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
    title = t.cancelRequested ? '正在停止执行器。' : t.desc || '任务正在执行。';
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
  if (!external && !t.cancelRequested && ['queued','running','review','failed'].includes(t.status)) buttons += actionButton('cancel', '取消任务');
  return `<div class="tw-callout ${esc(statusOrder.includes(t.status) ? t.status : '')}"><strong>${esc(title)}</strong>${description ? `<p style="margin-top:6px;font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere">${esc(description)}</p>` : ''}${buttons ? `<div class="tw-actions">${buttons}</div>` : ''}</div>`;
}
function detail() {
  const t = currentTask();
  if (!t) return '<div class="tw-loader">任务已不存在。<button class="tw-link" data-back="board">返回看板</button></div>';
  const plan = arr(t.plan);
  const children = tasks().filter(child => child.parentTaskId === t.id);
  const feedbackAllowed = ['review','failed','done'].includes(t.status) && !children.length;
  return `<div class="tw-detail"><div class="tw-detailhead"><button class="tw-link" data-back="board">← 返回任务看板</button><span class="tw-small">${esc(projectName(t.projectId))} / ${esc(t.id)}</span></div><div class="tw-flex tw-between tw-wrap"><h3>${esc(t.title)}</h3>${badge(t)}</div><div class="tw-engine" style="margin-top:10px">${icon('terminal')}${esc(agentName(t.engine))}<span>· ${esc(workerLabel(t))}</span></div><div class="tw-detailgrid"><div>${detailCallout(t)}<section class="tw-section"><h4>目标与验收标准</h4><p style="font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere">${esc(t.goal || t.desc)}</p>${criteriaHtml(t.criteria)}</section>${plan.length ? `<section class="tw-section"><h4>${t.status === 'review' ? '待确认的任务拆分' : '任务计划'}</h4><ol class="tw-steps">${plan.map((p, i) => `<li><span class="tw-n">${i + 1}</span><div><strong>${esc(p.title)}</strong>${p.goal ? `<p class="tw-plan-goal">${esc(p.goal)}</p>` : ''}<div class="tw-subnote">${arr(p.deps).length ? '依赖第 ' + arr(p.deps).map(d => Number(d) + 1).join('、') + ' 项' : '可独立开始'}</div>${criteriaHtml(p.criteria)}</div></li>`).join('')}</ol></section>` : ''}${steps(t).length ? `<section class="tw-section"><h4>已记录的进展</h4><ol class="tw-steps">${steps(t).map((s, i) => `<li class="${i < Number(t.done) ? 'complete' : ''}"><span class="tw-n">${i < Number(t.done) ? '✓' : i + 1}</span><div>${esc(s)}</div></li>`).join('')}</ol></section>` : ''}${arr(t.deps).length ? `<section class="tw-section"><h4>前序任务</h4>${arr(t.deps).map(id => `<button type="button" class="tw-dep" data-open="${esc(id)}">${esc(taskFor(id)?.title || id)} · ${esc(statuses[taskFor(id)?.status] || '未知状态')} ↗</button>`).join('')}</section>` : ''}${children.length ? `<section class="tw-section"><h4>子任务 <span class="tw-count">${children.length}</span></h4>${children.map(child => `<button type="button" class="tw-artifact" data-open="${esc(child.id)}"><span>${esc(child.title)}</span>${badge(child)}</button>`).join('')}</section>` : ''}${t.result ? `<section class="tw-section"><h4>执行结果</h4><div class="tw-result">${esc(t.result)}</div></section>` : ''}${feedbackAllowed ? `<form id="tw-feedback-form" class="tw-section"><label for="tw-feedback">给这个任务反馈</label><textarea id="tw-feedback" name="feedback" placeholder="说明哪些结果符合预期、哪里需要调整。" maxlength="12000" required></textarea><div class="tw-actions" style="margin-top:8px"><button type="submit" class="tw-btn tw-primary"${busy ? ' disabled' : ''}>${t.executionMode === 'external' ? '记录反馈' : '发送并继续'}</button><span class="tw-small">${t.executionMode === 'external' ? '保存到任务记录，由当前协作会话处理' : '保留已有记录，继续同一任务'}</span></div></form>` : ''}</div><aside><section class="tw-section"><h4>交付物 <span class="tw-count">${artifacts(t).length}</span></h4>${artifactList(t) || '<p class="tw-small">尚未生成产物。</p>'}</section><section class="tw-section"><h4>执行记录</h4><div class="tw-logs"><div class="tw-log">${arr(t.log).slice().reverse().map(l => `<div class="tw-logitem"><div class="tw-logtime">${esc(timeLabel(l[0]))}</div>${esc(l[1])}</div>`).join('') || '<p class="tw-small">等待第一条记录。</p>'}</div></div></section>${arr(t.runs).length ? `<section class="tw-section"><h4>执行轮次</h4>${arr(t.runs).slice().reverse().map((run, index) => `<details class="tw-run"><summary>第 ${arr(t.runs).length - index} 次 · ${esc(run.status || (run.ok === true ? '成功' : run.ok === false ? '失败' : '执行记录'))}</summary><pre>${esc(JSON.stringify(run, null, 2))}</pre></details>`).join('')}</section>` : ''}<section class="tw-section"><h4>任务信息</h4><p class="tw-small">创建于 ${esc(timeLabel(t.createdAt))}</p><p class="tw-small">更新于 ${esc(timeLabel(t.updatedAt))}</p>${t.parentTaskId ? `<button class="tw-link" data-open="${esc(t.parentTaskId)}" style="margin-top:8px">查看所属计划 →</button>` : ''}</section></aside></div></div>`;
}
function newTaskForm() {
  const selectedProject = drafts.get('new:')?.['tw-task-project'] || (project === 'all' ? state.projects[0]?.id : project);
  const engine = state.adapters.find(a => a.available && a.enabled)?.id;
  const dependencies = tasks().filter(t => t.projectId === selectedProject && t.status !== 'cancelled');
  return `<div class="tw-detail"><button class="tw-link" data-back="board">← 返回任务看板</button><form id="tw-new-form" class="tw-newform"><h3 style="margin-bottom:21px">你想完成什么？</h3><div class="tw-formgroup"><label for="tw-task-project">所属项目</label><select id="tw-task-project" name="projectId" required>${projectOptions(selectedProject)}</select><p class="tw-formhint">任务在此项目的本机目录中执行。</p></div><div class="tw-formgroup"><label for="tw-title">任务名称</label><input id="tw-title" name="title" maxlength="160" placeholder="例如：给任务管理器加入搜索功能" required></div><div class="tw-formgroup"><label for="tw-goal">目标和必要背景</label><textarea id="tw-goal" name="goal" maxlength="20000" placeholder="给谁用，解决什么问题，已有些什么？" required></textarea></div><div class="tw-formgroup"><label for="tw-criteria">怎样算完成？</label><textarea id="tw-criteria" name="criteria" maxlength="6000" placeholder="每行一项验收标准，例如：输入关键词，可以找到对应任务。" required></textarea></div><div class="tw-formgroup"><label for="tw-engine">使用哪个 Agent？</label><select id="tw-engine" name="engine">${agentOptions(engine)}</select>${!engine ? '<p class="tw-error">尚无可用执行器。可以先保存任务，配置完成后再启动。</p>' : '<p class="tw-subnote">使用本机已安装的 Agent；登录与权限按各 Agent 的设置执行。</p>'}</div><div class="tw-formgroup"><label for="tw-phase">执行方式</label><select id="tw-phase" name="phase"><option value="plan">先生成计划，确认后分发</option><option value="execute">目标明确，直接执行</option></select></div>${dependencies.length ? `<div class="tw-formgroup"><label for="tw-deps">前序任务（选填，可多选）</label><select id="tw-deps" name="deps" multiple size="${Math.min(dependencies.length, 4)}">${dependencies.map(t => `<option value="${esc(t.id)}">${esc(t.id + ' · ' + t.title + ' · ' + (statuses[t.status] || t.status))}</option>`).join('')}</select><p class="tw-formhint">只会在所选任务全部验收后开始。</p></div>` : ''}<div class="tw-callout"><h4>先确认方向，再推进任务</h4><p class="tw-subnote">“先生成计划”会等待你的批准；执行完成也需要验收，才会标记为已完成。</p></div><div class="tw-actions"><button type="submit" name="intent" value="start" class="tw-btn tw-primary"${busy || !engine ? ' disabled' : ''}>创建并启动 →</button><button type="submit" name="intent" value="save" class="tw-btn"${busy ? ' disabled' : ''}>先保存任务</button></div></form></div>`;
}
function newProjectForm() {
  return `<div class="tw-detail"><button class="tw-link" data-back="board">← 返回任务看板</button><form id="tw-project-form" class="tw-newform"><h3 style="margin-bottom:20px">新建项目</h3><div class="tw-formgroup"><label for="tw-project-name">项目名称</label><input id="tw-project-name" name="name" maxlength="100" placeholder="一个清楚、便于区分的名字" required></div><div class="tw-formgroup"><label for="tw-project-path">本机项目目录</label><input id="tw-project-path" name="path" placeholder="/Users/你的用户名/Projects/项目名" required><p class="tw-formhint">使用已存在的绝对路径。Agent 会在这个目录中读取和修改项目文件。</p></div><button type="submit" class="tw-btn tw-primary"${busy ? ' disabled' : ''}>创建项目</button></form></div>`;
}
function workerPage() {
  const running = tasks().filter(t => t.status === 'running');
  const external = running.filter(t => t.executionMode === 'external');
  const local = running.filter(t => t.executionMode !== 'external');
  const row = t => `<div class="tw-workerrow"><span class="tw-avatar busy">${icon('bot')}</span><div style="flex:1;min-width:0"><h4>${esc(workerLabel(t))}</h4><div class="tw-subnote">${esc(projectName(t.projectId))} · ${esc(t.title)}</div><div class="tw-subnote">${esc(t.desc || '正在推进')}</div></div><button class="tw-btn" data-open="${esc(t.id)}">查看任务</button></div>`;
  return `<div class="tw-detail"><button class="tw-link" data-back="board">← 返回任务看板</button><h3 style="margin-top:20px">执行者</h3><p class="tw-small" style="margin-top:5px">本机执行器最多同时运行 ${Number(state.maxWorkers) || 3} 个任务；需要你确认时释放执行位置。</p><section class="tw-section" style="margin-top:24px"><h4>协作 worker <span class="tw-count">${new Set(external.map(t => t.worker).filter(Boolean)).size}</span></h4><p class="tw-small">来自当前协作会话；由负责的 worker 汇报真实进展。</p>${external.map(row).join('') || '<div class="tw-empty" style="margin-top:12px">当前没有协作任务正在运行。</div>'}</section><section class="tw-section"><h4>本机 Agent <span class="tw-count">${local.length}/${Number(state.maxWorkers) || 3}</span></h4>${local.map(row).join('') || '<div class="tw-empty" style="margin-top:12px">执行位置空闲，等待已启动且满足依赖的任务。</div>'}</section></div>`;
}
const capabilityNames = {plan:'规划',execute:'执行',resume:'续接会话',stream:'流式记录',artifacts:'产物',cancel:'取消',readonly:'只读规划','read-only':'只读规划','structured-output':'结构化结果'};
function adapterPage() {
  return `<div class="tw-detail"><div class="tw-detailhead"><button class="tw-link" data-back="board">← 返回任务看板</button><span class="tw-badge">本机检测</span></div><h3>选择你的执行器</h3><p class="tw-small" style="margin-top:7px">同一套任务、反馈与产物，可以交给不同 Agent 执行。</p>${state.adapters.map(a => `<div class="tw-adapterrow"><span class="tw-avatar">${icon('terminal')}</span><div class="tw-adapterinfo"><h4>${esc(a.name)}</h4><div class="tw-subnote">${esc(a.description || '')}</div><div class="tw-subnote">${arr(a.capabilities).map(c => esc(capabilityNames[c] || c)).join(' · ')}</div>${a.executable ? `<div class="tw-projectpath" style="margin-top:6px">${esc(a.executable)}</div>` : ''}</div><span class="tw-badge ${a.available && a.enabled ? 'done' : 'failed'}">${!a.enabled ? '已停用' : a.available ? '已找到' : '未找到'}</span></div>`).join('')}<section class="tw-section" style="margin-top:24px"><h4>添加其他 Code Agent</h4><p style="font-size:12px">通过本机适配器配置文件接入其他执行器，重启服务后会在这里显示。新建任务时即可选择。</p><p class="tw-subnote">“已找到”表示检测到可执行程序；实际登录状态会在启动任务时确认。</p></section><section class="tw-section"><h4>从你习惯的入口开始</h4><p style="font-size:12px">网页、命令行和支持 MCP 的 Agent 共用同一套任务记录。</p></section></div>`;
}
function previewPage() {
  const t = currentTask();
  const a = t && artifacts(t)[preview?.index];
  if (!t || !a) return '<div class="tw-loader">找不到这份产物。<button class="tw-link" data-back="detail">返回任务</button></div>';
  return `<div class="tw-detail"><div class="tw-detailhead"><button class="tw-link" data-back="detail">← 返回任务</button><span class="tw-small">产物预览</span></div><h3>${esc(a.name)}</h3><div class="tw-subnote">来自任务：${esc(t.title)}</div>${preview.error ? `<div class="tw-error">${esc(preview.error)}</div><button class="tw-btn" data-artifact="${preview.index}" data-task="${esc(t.id)}">重新加载</button>` : `<div class="tw-preview">${esc(preview.loading ? '正在读取产物…' : preview.body || '文件内容为空。')}</div>`}</div>`;
}
function render() {
  if (!loaded) return;
  const snapshot = captureForm();
  header();
  const pages = {board,detail,new:newTaskForm,newProject:newProjectForm,workers:workerPage,adapters:adapterPage,preview:previewPage};
  $('#tw-content').innerHTML = (pages[page] || board)();
  renderedKey = `${page}:${selected || ''}`;
  setNotice(notice, noticeError);
  restoreForm(snapshot);
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
  await mutate(async () => {
    await api(`/api/tasks/${encodeURIComponent(id)}/actions`, {method:'POST',body:JSON.stringify({action,...payload})});
    const messages = {start:'任务已加入队列，进展会自动更新。',retry:'已提交重试，历史记录保留。',approve_plan:'计划已批准，子任务将按依赖开始执行。',accept:'已验收通过，后续依赖任务可以继续。',feedback:'反馈已保存到任务记录。',cancel:'已提交取消请求，进程停止后会更新状态。'};
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
  if (d.open) { navigate('detail', d.open); return; }
  if (d.filter) { captureForm(); filter = d.filter; page = 'board'; selected = null; setNotice(''); saveView(); render(); return; }
  if (d.layout) { captureForm(); view = d.layout; page = 'board'; selected = null; saveView(); render(); return; }
  if (d.back) { navigate(d.back, d.back === 'board' ? null : selected); return; }
  if ('artifact' in d) { loadArtifact(d.task || selected, Number(d.artifact)); return; }
  if (button.id === 'tw-new' || button.id === 'tw-first-task') { navigate('new', null); $('#tw-title')?.focus(); return; }
  if (button.id === 'tw-adapters') { navigate('adapters', null); return; }
  if (button.id === 'tw-workers') { navigate('workers', null); return; }
  if ('focusFeedback' in d) { $('#tw-feedback')?.focus(); $('#tw-feedback')?.scrollIntoView({block:'center',behavior:'smooth'}); return; }
  if (d.action) taskAction(d.action);
});
root.addEventListener('change', event => {
  if (event.target.id === 'tw-project-filter') {
    captureForm();
    if (event.target.value === '__new__') { page = 'newProject'; selected = null; setNotice(''); render(); $('#tw-project-name')?.focus(); return; }
    project = event.target.value; page = 'board'; selected = null; filter = 'all'; setNotice(''); saveView(); render();
  } else if (event.target.id === 'tw-task-project') {
    captureForm();
    const draft = drafts.get('new:');
    if (draft) draft['tw-deps'] = [];
    render();
  }
});
root.addEventListener('submit', event => {
  event.preventDefault();
  if (busy) return;
  const form = event.target;
  if (!form.reportValidity()) return;
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
      executionMode:'local',scheduled:false,start,
      deps:Array.from($('#tw-deps')?.selectedOptions || []).map(o => o.value),
    };
    if (!data.title || !data.goal || !data.criteria.length) { setNotice('请填写任务名称、目标和验收标准。', true); return; }
    if (start && (!adapterFor(engine)?.available || !adapterFor(engine)?.enabled)) { setNotice('请选择可用的执行器，或先保存任务。', true); return; }
    mutate(async () => {
      const task = await api('/api/tasks', {method:'POST',body:JSON.stringify(data)});
      drafts.delete('new:'); renderedKey = null;
      // Leave the form before rerendering so its cleared draft is not recaptured.
      page = 'detail'; selected = task.id; project = task.projectId; filter = 'all'; saveView();
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
