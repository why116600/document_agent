/* =====================================================================
   文档智能体 Web 前端逻辑（原生 JS，无框架、无构建步骤）
   - 会话：新建 / 切换 / 重命名 / 删除
   - 文件：上传（含拖拽）/ 下载 / 删除
   - 生成：提交需求 → SSE 实时日志 → 结果刷新与下载
   ===================================================================== */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);

  const state = {
    sessions: [],
    sessionId: null,
    detail: null,
    busy: false,
    jobId: null,
    es: null,
    logs: new Map(), // jobId -> string[]，页面刷新后仅保留内存中的日志
    warnedKey: false,
  };

  const nodes = {
    app: document.querySelector('.app'),
    sidebar: $('sidebar'),
    sidebarMask: $('sidebar-mask'),
    openSidebar: $('open-sidebar'),
    closeSidebar: $('close-sidebar'),
    newSession: $('new-session'),
    emptyNewSession: $('empty-new-session'),
    sessionCount: $('session-count'),
    sessionList: $('session-list'),
    serverStatus: $('server-status'),
    statusText: $('server-status').querySelector('.text'),
    knowledgeStatus: $('knowledge-status'),
    sessionTitle: $('session-title'),
    sessionPath: $('session-path'),
    deleteSession: $('delete-session'),
    emptyState: $('empty-state'),
    sessionBody: $('session-body'),
    fileCount: $('file-count'),
    fileList: $('file-list'),
    fileInput: $('file-input'),
    dropzone: $('dropzone'),
    refreshBtn: $('refresh-btn'),
    chatEmpty: $('chat-empty'),
    messages: $('messages'),
    prompt: $('prompt'),
    send: $('send'),
    toastStack: $('toast-stack'),
  };

  // ------------------------------------------------------------------ 基础工具
  async function api(path, { method = 'GET', body = null, headers = {} } = {}) {
    const options = { method, headers: { ...headers } };
    if (body instanceof FormData) {
      options.body = body;
    } else if (body !== null && body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    if (response.status === 204) return null;
    const text = await response.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch { data = text; }
    }
    if (!response.ok) {
      const detail = data && data.detail;
      let message;
      if (typeof detail === 'string') message = detail;
      else if (Array.isArray(detail)) message = detail.map((item) => item.msg || JSON.stringify(item)).join('；');
      else if (detail) message = JSON.stringify(detail);
      else message = `请求失败（HTTP ${response.status}）`;
      throw new Error(message);
    }
    return data;
  }

  function toast(message, type = 'info', timeout = 4500) {
    const item = document.createElement('div');
    item.className = `toast ${type}`;
    item.textContent = message;
    item.addEventListener('click', () => item.remove());
    nodes.toastStack.appendChild(item);
    setTimeout(() => {
      item.classList.add('leaving');
      setTimeout(() => item.remove(), 220);
    }, timeout);
  }

  function fmtTime(value) {
    if (!value) return '';
    // 后端格式：YYYY-MM-DD HH:MM:SS
    return String(value).slice(5, 16);
  }

  function sessionUrl(suffix = '') {
    return `/api/sessions/${encodeURIComponent(state.sessionId)}${suffix}`;
  }

  function fileUrl(name) {
    return sessionUrl(`/files/${encodeURIComponent(name)}`);
  }

  function autoResize() {
    const textarea = nodes.prompt;
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
  }

  function scrollChatToBottom() {
    nodes.messages.scrollTop = nodes.messages.scrollHeight;
  }

  // ------------------------------------------------------------------ 健康状态
  async function loadHealth() {
    try {
      const health = await api('/api/health');
      const configured = Boolean(health.llm_configured);
      nodes.serverStatus.className = `status-line ${configured ? 'ok' : 'warn'}`;
      nodes.statusText.textContent = configured ? '服务运行中 · 大模型已配置' : '服务运行中 · 未配置 API Key';
      nodes.knowledgeStatus.textContent = health.knowledge ? health.knowledge.message : '';
      if (!configured && !state.warnedKey) {
        state.warnedKey = true;
        toast('未检测到 MODEL_API_KEY，生成文档会失败。请设置环境变量后重启服务。', 'warn', 9000);
      }
    } catch (error) {
      nodes.serverStatus.className = 'status-line error';
      nodes.statusText.textContent = '无法连接服务';
      nodes.knowledgeStatus.textContent = '请确认后端已启动';
    }
  }

  // ------------------------------------------------------------------ 会话列表
  async function loadSessions() {
    const data = await api('/api/sessions');
    state.sessions = data.sessions || [];
    renderSessions();
  }

  function renderSessions() {
    nodes.sessionList.innerHTML = '';
    nodes.sessionCount.textContent = String(state.sessions.length);
    if (!state.sessions.length) {
      const empty = document.createElement('div');
      empty.className = 'sidebar-empty';
      empty.textContent = '还没有会话，点击上方「新建会话」开始。';
      nodes.sessionList.appendChild(empty);
      return;
    }
    state.sessions.forEach((session) => {
      const item = document.createElement('div');
      item.className = `session-item${session.id === state.sessionId ? ' active' : ''}`;
      item.setAttribute('role', 'button');
      item.tabIndex = 0;
      item.addEventListener('click', () => selectSession(session.id));
      item.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          selectSession(session.id);
        }
      });

      const title = document.createElement('div');
      title.className = 'session-item-title';
      title.textContent = session.title || session.id;
      title.title = session.title || session.id;

      const meta = document.createElement('div');
      meta.className = 'session-item-meta';
      const time = document.createElement('span');
      time.textContent = fmtTime(session.updated_at);
      const files = document.createElement('span');
      files.textContent = `${session.file_count || 0} 个文件`;
      meta.append(time, files);
      if (session.running) {
        const running = document.createElement('span');
        running.className = 'running';
        running.textContent = '生成中…';
        meta.appendChild(running);
      }

      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'session-del';
      del.title = '删除会话';
      del.textContent = '×';
      del.addEventListener('click', (event) => {
        event.stopPropagation();
        deleteSession(session);
      });

      item.append(title, meta, del);
      nodes.sessionList.appendChild(item);
    });
  }

  // ------------------------------------------------------------------ 会话详情
  async function selectSession(sessionId) {
    if (state.sessionId === sessionId && state.detail) return;
    closeStream();
    state.sessionId = sessionId;
    renderSessions();
    closeSidebar();
    try {
      state.detail = await api(sessionUrl());
    } catch (error) {
      state.detail = null;
      toast(error.message, 'error');
      renderSession();
      return;
    }
    renderSession();
    resumeRunningJob();
  }

  async function refreshSession() {
    if (!state.sessionId) return;
    state.detail = await api(sessionUrl());
    renderSession();
    try { await loadSessions(); } catch { /* 列表刷新失败不影响主流程 */ }
  }

  function renderSession() {
    const detail = state.detail;
    const hasSession = Boolean(detail);
    nodes.emptyState.hidden = hasSession;
    nodes.sessionBody.hidden = !hasSession;
    nodes.sessionTitle.disabled = !hasSession || state.busy;
    nodes.deleteSession.disabled = !hasSession;

    if (!hasSession) {
      nodes.sessionTitle.value = '';
      nodes.sessionPath.textContent = '';
      nodes.fileList.innerHTML = '';
      nodes.fileCount.textContent = '0';
      nodes.messages.innerHTML = '';
      closeStream();
      setBusy(false);
      return;
    }

    nodes.sessionTitle.value = detail.title || '';
    nodes.sessionPath.textContent = detail.path || '';
    renderFiles(detail.files || []);
    renderMessages(detail.messages || []);
  }

  function renderFiles(files) {
    nodes.fileList.innerHTML = '';
    nodes.fileCount.textContent = String(files.length);
    if (!files.length) {
      const empty = document.createElement('li');
      empty.className = 'files-empty';
      empty.textContent = '文件夹还是空的：上传参考资料，或者直接在下方描述需求开始写作。';
      nodes.fileList.appendChild(empty);
      return;
    }
    files.forEach((file) => {
      const row = document.createElement('li');
      row.className = 'file-row';

      const icon = document.createElement('span');
      icon.className = `file-icon ${file.suffix || 'other'}`;
      icon.textContent = (file.suffix || 'file').slice(0, 4);

      const info = document.createElement('div');
      info.className = 'file-info';
      const link = document.createElement('a');
      link.className = 'file-name';
      link.href = fileUrl(file.name);
      link.download = file.name;
      link.textContent = file.name;
      link.title = `下载 ${file.name}`;
      const meta = document.createElement('div');
      meta.className = 'file-meta';
      if (file.kind === 'generated') {
        const badge = document.createElement('span');
        badge.className = 'badge';
        badge.textContent = '生成文档';
        meta.appendChild(badge);
      }
      const infoText = document.createElement('span');
      infoText.textContent = `${file.size_text} · ${file.modified}`;
      meta.appendChild(infoText);
      info.append(link, meta);

      const actions = document.createElement('div');
      actions.className = 'file-actions';
      const download = document.createElement('a');
      download.className = 'file-action';
      download.href = fileUrl(file.name);
      download.download = file.name;
      download.textContent = '下载';
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'file-action del';
      remove.textContent = '删除';
      remove.addEventListener('click', () => deleteFile(file.name));
      actions.append(download, remove);

      row.append(icon, info, actions);
      nodes.fileList.appendChild(row);
    });
  }

  function renderMessages(messages) {
    nodes.messages.innerHTML = '';
    nodes.chatEmpty.hidden = messages.length > 0;
    messages.forEach((message) => nodes.messages.appendChild(buildMessage(message)));
    scrollChatToBottom();
  }

  function buildMessage(message) {
    const wrap = document.createElement('div');
    wrap.className = `msg ${message.role === 'user' ? 'user' : 'assistant'}`;
    wrap.dataset.msgId = message.id || '';

    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.textContent = message.role === 'user' ? '我' : 'AI';

    const bubble = document.createElement('div');
    bubble.className = 'bubble';

    if (message.role === 'user') {
      bubble.textContent = message.content || '';
    } else {
      bubble.appendChild(buildAssistantHead(message));
      if (message.content) {
        const text = document.createElement('p');
        text.className = 'msg-text';
        text.textContent = message.content;
        bubble.appendChild(text);
      }
      if (message.error) {
        const error = document.createElement('div');
        error.className = 'msg-error';
        error.textContent = message.error;
        bubble.appendChild(error);
      }
      if (Array.isArray(message.files) && message.files.length) {
        const chips = document.createElement('div');
        chips.className = 'file-chips';
        message.files.forEach((name) => {
          const chip = document.createElement('a');
          chip.className = 'file-chip';
          chip.href = fileUrl(name);
          chip.download = name;
          chip.title = `下载 ${name}`;
          const chipIcon = document.createElement('span');
          chipIcon.className = 'chip-icon';
          chipIcon.textContent = 'W';
          const label = document.createElement('span');
          label.textContent = name;
          chip.append(chipIcon, label);
          chips.appendChild(chip);
        });
        bubble.appendChild(chips);
      }
      const logBlock = buildLogBlock(message);
      if (logBlock) bubble.appendChild(logBlock);
    }

    wrap.append(avatar, bubble);
    return wrap;
  }

  function buildAssistantHead(message) {
    const head = document.createElement('div');
    head.className = 'msg-head';
    const status = document.createElement('span');
    status.className = 'msg-status';

    if (message.status === 'running') {
      const spinner = document.createElement('span');
      spinner.className = 'spinner';
      const text = document.createElement('span');
      text.className = 'run-status';
      text.dataset.jobId = message.job_id || '';
      text.textContent = '正在生成文档…';
      status.append(spinner, text);
    } else if (message.status === 'failed') {
      status.classList.add('err');
      status.textContent = '生成失败';
    } else {
      status.classList.add('ok');
      status.textContent = '生成完成';
    }
    head.appendChild(status);
    return head;
  }

  function buildLogBlock(message) {
    const jobId = message.job_id;
    if (!jobId) return null;
    const lines = state.logs.get(jobId) || [];
    const running = message.status === 'running';
    if (!lines.length && !running && !message.log_file) return null;

    const details = document.createElement('details');
    details.className = 'log-block';
    if (running) details.open = true;

    const summary = document.createElement('summary');
    const label = document.createElement('span');
    label.textContent = running ? '实时运行日志' : '运行日志';
    const count = document.createElement('span');
    count.className = 'log-count';
    count.dataset.jobId = jobId;
    count.textContent = `${lines.length} 行`;
    summary.append(label, count);

    if (message.log_file && !running) {
      const link = document.createElement('a');
      link.href = sessionUrl(`/logs/${encodeURIComponent(jobId)}`);
      link.target = '_blank';
      link.rel = 'noopener';
      link.className = 'file-action';
      link.textContent = '查看完整日志';
      link.style.marginLeft = 'auto';
      link.addEventListener('click', (event) => event.stopPropagation());
      summary.appendChild(link);
    }

    details.appendChild(summary);
    if (lines.length || running) {
      const body = document.createElement('div');
      body.className = 'log-body';
      body.dataset.jobId = jobId;
      if (lines.length) {
        body.textContent = `${lines.slice(-2000).join('\n')}\n`;
      }
      details.appendChild(body);
      if (lines.length) {
        requestAnimationFrame(() => { body.scrollTop = body.scrollHeight; });
      }
    } else {
      // 页面刷新后日志只留在磁盘上：不渲染空的日志区，摘要行里的链接仍可查看完整日志
      const body = document.createElement('div');
      body.className = 'log-body';
      body.textContent = '完整日志已保存，点击上方「查看完整日志」打开。\n';
      details.appendChild(body);
    }
    return details;
  }

  // ------------------------------------------------------------------ 任务事件流
  function setBusy(busy) {
    state.busy = busy;
    nodes.app.classList.toggle('busy', busy);
    nodes.send.disabled = busy;
    nodes.send.textContent = busy ? '生成中…' : '发送';
    nodes.fileInput.disabled = busy;
    nodes.sessionTitle.disabled = busy || !state.detail;
  }

  function closeStream() {
    if (state.es) {
      try { state.es.close(); } catch { /* ignore */ }
      state.es = null;
    }
    state.jobId = null;
  }

  function watchJob(jobId) {
    closeStream();
    state.jobId = jobId;
    setBusy(true);
    const source = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
    state.es = source;

    source.onmessage = (event) => {
      let data = null;
      try { data = JSON.parse(event.data); } catch { return; }
      if (data.type === 'log') {
        appendLog(jobId, data.line);
      } else if (data.type === 'status') {
        setRunStatus(jobId, data.message);
      } else if (data.type === 'done') {
        closeStream();
        finishJob(jobId, data.result);
      } else if (data.type === 'error') {
        closeStream();
        failJob(jobId, data.message);
      }
    };

    source.onerror = () => {
      // 正常结束时我们已主动 close；这里只处理异常断线，稍后按服务端记录的最终状态收敛
      if (source.readyState === EventSource.CLOSED) {
        closeStream();
        window.setTimeout(() => {
          if (state.sessionId) refreshSession().then(resumeRunningJob).catch(() => {});
        }, 800);
      }
    };
  }

  async function attachJob(jobId) {
    try {
      const info = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (info.status === 'running') {
        watchJob(jobId);
      } else {
        // 任务已结束但消息还没刷新（例如页面刷新较晚）
        await refreshSession();
        setBusy(false);
      }
    } catch {
      markInterrupted(jobId);
      setBusy(false);
    }
  }

  function resumeRunningJob() {
    const messages = state.detail ? state.detail.messages || [] : [];
    const running = messages.find((item) => item.role === 'assistant' && item.status === 'running' && item.job_id);
    if (!running) {
      setBusy(false);
      return;
    }
    setBusy(true);
    attachJob(running.job_id);
  }

  function appendLog(jobId, line) {
    let lines = state.logs.get(jobId);
    if (!lines) {
      lines = [];
      state.logs.set(jobId, lines);
    }
    lines.push(line);
    if (lines.length > 4000) lines.splice(0, lines.length - 4000);

    const body = document.querySelector(`.log-body[data-job-id="${jobId}"]`);
    if (!body) return;
    body.appendChild(document.createTextNode(`${line}\n`));
    while (body.childNodes.length > 2000) body.removeChild(body.firstChild);
    const counter = document.querySelector(`.log-count[data-job-id="${jobId}"]`);
    if (counter) counter.textContent = `${lines.length} 行`;
    if (body.scrollTop + body.clientHeight >= body.scrollHeight - 80) {
      body.scrollTop = body.scrollHeight;
    }
  }

  function setRunStatus(jobId, text) {
    const node = document.querySelector(`.run-status[data-job-id="${jobId}"]`);
    if (node && text) node.textContent = text;
  }

  function markInterrupted(jobId) {
    const node = document.querySelector(`.run-status[data-job-id="${jobId}"]`);
    if (node) node.textContent = '任务已中断（服务可能已重启），请重新发送需求';
  }

  async function finishJob(jobId, result) {
    setBusy(false);
    const file = result && result.file;
    toast(file ? `文档已生成：${file}` : '文档已生成', 'success', 6000);
    try {
      await refreshSession();
      scrollChatToBottom();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function failJob(jobId, message) {
    setBusy(false);
    toast(message || '生成失败', 'error', 8000);
    try {
      await refreshSession();
    } catch { /* ignore */ }
  }

  // ------------------------------------------------------------------ 操作
  async function createSession() {
    try {
      const session = await api('/api/sessions', { method: 'POST', body: {} });
      await loadSessions();
      state.sessionId = null;
      state.detail = null;
      await selectSession(session.id);
      closeSidebar();
      nodes.prompt.focus();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function deleteSession(session) {
    const title = session.title || session.id;
    if (!window.confirm(`确定删除会话「${title}」吗？会话文件夹中的文件会一起删除，且不可恢复。`)) return;
    try {
      await api(`/api/sessions/${encodeURIComponent(session.id)}`, { method: 'DELETE' });
      if (state.sessionId === session.id) {
        closeStream();
        state.sessionId = null;
        state.detail = null;
        renderSession();
        await loadSessions();
        if (state.sessions.length) await selectSession(state.sessions[0].id);
      } else {
        await loadSessions();
      }
      toast('会话已删除', 'success');
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function deleteCurrentSession() {
    if (!state.detail) return;
    await deleteSession({ id: state.detail.id, title: state.detail.title });
  }

  async function uploadFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    if (!state.sessionId) {
      toast('请先新建或选择一个会话', 'warn');
      return;
    }
    if (state.busy) {
      toast('正在生成文档，请等任务结束后再上传', 'warn');
      return;
    }
    const form = new FormData();
    files.forEach((file) => form.append('files', file, file.name));
    toast(`正在上传 ${files.length} 个文件…`, 'info', 2500);
    try {
      const result = await api(sessionUrl('/files'), { method: 'POST', body: form });
      if (result.errors && result.errors.length) {
        toast(result.errors.join('；'), 'warn', 8000);
      }
      if (result.files && result.files.length) {
        toast(`已上传 ${result.files.length} 个文件`, 'success');
      }
      await refreshSession();
    } catch (error) {
      toast(error.message, 'error', 8000);
    }
  }

  async function deleteFile(name) {
    if (state.busy) {
      toast('正在生成文档，请稍后再删除文件', 'warn');
      return;
    }
    if (!window.confirm(`确定删除文件「${name}」吗？`)) return;
    try {
      await api(fileUrl(name), { method: 'DELETE' });
      toast('文件已删除', 'success');
      await refreshSession();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function sendMessage() {
    const text = nodes.prompt.value.trim();
    if (!text) {
      toast('请先描述你的写作需求', 'warn');
      return;
    }
    if (!state.sessionId) {
      toast('请先新建或选择一个会话', 'warn');
      return;
    }
    if (state.busy) return;

    nodes.prompt.value = '';
    autoResize();
    setBusy(true);
    try {
      const result = await api(sessionUrl('/messages'), {
        method: 'POST',
        body: { content: text },
      });
      await refreshSession();
      watchJob(result.job_id);
      scrollChatToBottom();
    } catch (error) {
      setBusy(false);
      nodes.prompt.value = text;
      autoResize();
      toast(error.message, 'error', 8000);
    }
  }

  let titleSaving = false;
  async function commitTitle() {
    if (!state.detail || titleSaving || state.busy) return;
    const value = nodes.sessionTitle.value.trim();
    if (!value || value === state.detail.title) {
      nodes.sessionTitle.value = state.detail.title || '';
      return;
    }
    titleSaving = true;
    try {
      const meta = await api(sessionUrl(), { method: 'PATCH', body: { title: value } });
      state.detail.title = meta.title;
      nodes.sessionTitle.value = meta.title;
      await loadSessions();
      toast('标题已更新', 'success', 2000);
    } catch (error) {
      nodes.sessionTitle.value = state.detail.title || '';
      toast(error.message, 'error');
    } finally {
      titleSaving = false;
    }
  }

  // ------------------------------------------------------------------ 侧边栏（窄屏）
  function openSidebar() {
    nodes.sidebar.classList.add('open');
    nodes.sidebarMask.classList.add('show');
  }
  function closeSidebar() {
    nodes.sidebar.classList.remove('open');
    nodes.sidebarMask.classList.remove('show');
  }

  // ------------------------------------------------------------------ 事件绑定
  function bindEvents() {
    nodes.newSession.addEventListener('click', createSession);
    nodes.emptyNewSession.addEventListener('click', createSession);
    nodes.deleteSession.addEventListener('click', deleteCurrentSession);
    nodes.refreshBtn.addEventListener('click', () => {
      refreshSession().catch((error) => toast(error.message, 'error'));
    });

    nodes.fileInput.addEventListener('change', async (event) => {
      await uploadFiles(event.target.files);
      event.target.value = '';
    });

    ['dragenter', 'dragover'].forEach((name) => {
      nodes.dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        event.stopPropagation();
        nodes.dropzone.classList.add('drag');
      });
    });
    ['dragleave', 'dragend'].forEach((name) => {
      nodes.dropzone.addEventListener(name, (event) => {
        event.preventDefault();
        event.stopPropagation();
        nodes.dropzone.classList.remove('drag');
      });
    });
    nodes.dropzone.addEventListener('drop', async (event) => {
      event.preventDefault();
      event.stopPropagation();
      nodes.dropzone.classList.remove('drag');
      await uploadFiles(event.dataTransfer.files);
    });
    // 避免把文件拖到页面其它位置时浏览器直接打开文件
    ['dragover', 'drop'].forEach((name) => {
      window.addEventListener(name, (event) => event.preventDefault());
    });

    nodes.prompt.addEventListener('input', autoResize);
    nodes.prompt.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        sendMessage();
      }
    });
    nodes.send.addEventListener('click', sendMessage);

    nodes.sessionTitle.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        nodes.sessionTitle.blur();
      } else if (event.key === 'Escape') {
        nodes.sessionTitle.value = state.detail ? state.detail.title : '';
        nodes.sessionTitle.blur();
      }
    });
    nodes.sessionTitle.addEventListener('blur', commitTitle);

    nodes.openSidebar.addEventListener('click', openSidebar);
    nodes.closeSidebar.addEventListener('click', closeSidebar);
    nodes.sidebarMask.addEventListener('click', closeSidebar);

    window.addEventListener('beforeunload', closeStream);
  }

  // ------------------------------------------------------------------ 启动
  async function init() {
    bindEvents();
    await loadHealth();
    try {
      await loadSessions();
    } catch (error) {
      toast(`加载会话列表失败：${error.message}`, 'error', 8000);
    }
    if (state.sessions.length) {
      await selectSession(state.sessions[0].id);
    } else {
      renderSession();
    }
    autoResize();
    // 知识库是后台初始化的，定时刷新状态即可
    window.setInterval(loadHealth, 30000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
