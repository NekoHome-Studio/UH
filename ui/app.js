const chat = document.getElementById('chat');
const prompt = document.getElementById('prompt');
const send = document.getElementById('send');
let sessionId = null, busy = false;
let uiState = UHStateMachine.initialState();
let lastEventId = 0, reconnectAttempts = 0, terminalReached = false;
let desktopStatus = 'idle', desktopReports = Promise.resolve();

// 鉴权 token：URL ?token= 优先并存 localStorage；右上角输入框可改（生产鉴权模式 B）
const tokenInput = document.getElementById('token');
const urlToken = new URLSearchParams(location.search).get('token');
let authToken = urlToken || localStorage.getItem('uh_token') || '';
if (urlToken) localStorage.setItem('uh_token', urlToken);
tokenInput.value = authToken;
const reloadThemes = UHThemes.mountThemePicker(document.getElementById('theme'), document.getElementById('theme-status'), () => authToken);
tokenInput.addEventListener('change', () => { authToken = tokenInput.value.trim(); localStorage.setItem('uh_token', authToken); sessionId = null; reloadThemes(); });
function withToken(url) { return authToken ? url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(authToken) : url; }

const DOMAIN = {
  USR: '请求/认证问题，请检查输入或重新登录',
  KRN: '系统故障，请附 trace_id 反馈',
  CAP: '能力层问题，可稍后重试',
  MEM: '记忆/会话已过期，请新开会话'
};
function escapeHtml(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function addMsg(role, text) {
  const d = document.createElement('div');
  d.className = 'msg ' + role; d.textContent = text;
  chat.appendChild(d); d.scrollIntoView({behavior:'smooth'}); return d;
}
function breathing(on) {
  let b = document.getElementById('breath');
  if (on && !b) { b = document.createElement('div'); b.id='breath'; b.className='breathing';
    b.innerHTML='<span class="dot"></span>思考中…'; chat.appendChild(b); b.scrollIntoView({behavior:'smooth'}); }
  if (!on && b) b.remove();
}
function degradedBadge(on) {
  let b = document.getElementById('degraded');
  if (on && !b) { b = document.createElement('div'); b.id='degraded'; b.className='degraded';
    b.textContent='⚠ 降级运行（部分能力不可用）'; chat.appendChild(b); }
  if (!on && b) b.remove();
}
function reportDesktop(status, notice = null) {
  const core = window.__TAURI__?.core;
  if (!core || (status === desktopStatus && !notice)) return;
  desktopStatus = status;
  desktopReports = desktopReports.then(() => core.invoke('report_task_state', {status, notice}))
    .catch(error => console.warn('UH! Desktop', error));
}
function renderState(previous, event) {
  const running = !uiState.terminal && uiState.state !== 'idle';
  breathing(running);
  degradedBadge(!!uiState.degraded);  // 契约 C10：degraded 徽标
  let notice = null;
  if (!previous.terminal && uiState.terminal) {
    if (event === 'task.finished') notice = 'finished';
    if (event === 'task.failed') notice = 'failed';
  }
  reportDesktop(running ? (uiState.degraded ? 'degraded' : 'running') : 'idle', notice);
}
function showError(code, message, traceId) {
  const domain = (code||'').split('-')[1] || 'KRN';
  const d = document.createElement('div'); d.className='error '+domain;
  d.textContent = '['+code+'] '+(DOMAIN[domain]||'')+' — '+(message||'')+(traceId?' (trace: '+traceId+')':'');
  chat.appendChild(d); d.scrollIntoView({behavior:'smooth'});
}
function renderSteps(taskId) {
  fetch(withToken('/api/v1/tasks/'+taskId+'/steps')).then(r => r.ok ? r.json() : null).then(j => {
    if (!j || !j.data) return;
    const steps = j.data.steps || [];
    if (!steps.length) return;
    const d = document.createElement('div'); d.className='steps';
    d.textContent = '阶段（超窗回退重建）：' + steps.map(s => s.name+'['+s.status+']').join(' → ');
    chat.appendChild(d); d.scrollIntoView({behavior:'smooth'});
  }).catch(() => null);
}
function showConfirmation(ev, taskId, traceId) {
  const p = ev.payload;
  const d = document.createElement('div'); d.className='confirm';
  d.innerHTML = '<div class="confirm-title">⚠ 高危工具需确认：'+escapeHtml(p.tool_name)+'（'+escapeHtml(p.risk_level)+'）</div>'
    + '<div class="confirm-summary">'+escapeHtml(p.summary||'')+'</div>';
  const approve = document.createElement('button'); approve.textContent='批准执行'; approve.className='approve';
  const reject = document.createElement('button'); reject.textContent='拒绝'; reject.className='reject';
  async function decide(decision) {
    approve.disabled = reject.disabled = true;
    try {
      const r = await fetch(withToken('/api/v1/tasks/'+taskId+'/confirm'), {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({confirmation_token:p.confirmation_token, decision, operator:'ui-user'})});
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { showError((j.error&&j.error.code)||'UH-USR-1003', (j.error&&j.error.message)||'确认失败', traceId); approve.disabled = reject.disabled = false; }
    } catch (e) { showError('UH-KRN-5001', String(e), traceId); approve.disabled = reject.disabled = false; }
  }
  approve.onclick = () => decide('approved');
  reject.onclick = () => decide('rejected');
  d.appendChild(approve); d.appendChild(reject);
  chat.appendChild(d); d.scrollIntoView({behavior:'smooth'});
}
function handleEvent(ev, target, taskId, traceId) {
  lastEventId = ev.event_id || lastEventId;
  const previous = uiState;
  uiState = UHStateMachine.transition(uiState, ev);
  renderState(previous, ev.event);
  if (ev.event === 'delta') { target.textContent += ev.payload.text; target.scrollIntoView({behavior:'smooth'}); }
  else if (ev.event === 'task.confirmation_required') { showConfirmation(ev, taskId, traceId); }
  else if (ev.event === 'task.degraded') { degradedBadge(true); }
  else if (ev.event === 'task.finished') { terminalReached=true; breathing(false); busy=false; send.disabled=false; prompt.focus(); }
  else if (ev.event === 'task.failed') { terminalReached=true; breathing(false); target.remove(); showError(ev.payload.error.code, ev.payload.error.message, traceId); busy=false; send.disabled=false; }
  else if (ev.event === 'task.cancelled') { terminalReached=true; breathing(false); showError('UH-USR-1002', '任务已取消：'+(ev.payload.reason||''), traceId); busy=false; send.disabled=false; }
}
function connectWs(taskId, target, traceId, afterEventId) {
  const wsScheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = wsScheme + '//' + location.host + '/api/v1/stream?task_id=' + encodeURIComponent(taskId) + '&after_event_id=' + afterEventId + (authToken ? '&token=' + encodeURIComponent(authToken) : '');
  const ws = new WebSocket(wsUrl);
  ws.onopen = () => ws.send(JSON.stringify({op:'subscribe', task_id:taskId, after_event_id:afterEventId}));
  ws.onmessage = (e) => {
    let frame; try { frame = JSON.parse(e.data); } catch { return; }
    if (frame.op === 'pong') return;
    if (frame.op === 'error') { breathing(false); reportDesktop('degraded'); showError(frame.error.code, frame.error.message, traceId); ws.close(1000); busy=false; send.disabled=false; return; }
    if (frame.op === 'replay_unavailable') { renderSteps(taskId); return; }
    handleEvent(frame, target, taskId, traceId);
  };
  ws.onclose = (event) => {
    breathing(false);
    if ([4408, 4401, 4403].includes(event.code)) reportDesktop('degraded');
    if (event.code === 4408) { renderSteps(taskId); busy=false; send.disabled=false; return; }
    if (event.code === 4401) { showError('UH-USR-1001', '认证失败，请重新登录', traceId); busy=false; send.disabled=false; return; }
    if (event.code === 4403) { showError('UH-USR-1002', '无权订阅任务', traceId); busy=false; send.disabled=false; return; }
    if (event.code === 1000 || terminalReached) { busy=false; send.disabled=false; return; }
    if (reconnectAttempts < 3) { reconnectAttempts++; setTimeout(() => connectWs(taskId, target, traceId, lastEventId), 500 * (2 ** reconnectAttempts)); }
    else { reportDesktop('degraded'); showError('UH-KRN-5001', '事件流断开，重连失败', traceId); busy=false; send.disabled=false; }
  };
}
async function submit() {
  const content = prompt.value.trim();
  if (!content || busy) return;
  busy = true; send.disabled = true; prompt.value='';
  addMsg('user', content);
  try {
    if (!sessionId) {
      const sr = await fetch(withToken('/api/v1/sessions'), {method:'POST'});
      if (sr.status === 401) { showError('UH-USR-1001', '需要访问 token，请在右上角输入后重试', null); tokenInput.focus(); busy=false; send.disabled=false; return; }
      if (!sr.ok) throw new Error('创建会话失败 HTTP '+sr.status);
      sessionId = (await sr.json()).data.session_id;
    }
    const resp = await fetch(withToken('/api/v1/sessions/'+sessionId+'/messages'), {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({content, mode:'auto'})
    });
    if (!resp.ok) { const j = await resp.json().catch(()=>({})); showError((j.error&&j.error.code)||'UH-USR-1003', (j.error&&j.error.message)||'发送失败', null); busy=false; send.disabled=false; return; }
    const { task_id, trace_id } = (await resp.json()).data;
    const target = addMsg('assistant', '');
    uiState = UHStateMachine.initialState();
    terminalReached = false; reconnectAttempts = 0; lastEventId = 0;
    connectWs(task_id, target, trace_id, 0);
  } catch (err) {
    breathing(false); showError('UH-KRN-5001', String(err), null); busy=false; send.disabled=false;
  }
}
send.addEventListener('click', submit);
prompt.addEventListener('keydown', e => { if (e.key==='Enter') submit(); });
