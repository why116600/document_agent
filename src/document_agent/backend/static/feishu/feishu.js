/* =====================================================================
   飞书版前端的登录门禁：
   - 先向 /api/feishu/auth/status 查询登录态；
   - 未配置飞书应用时给出配置提示；
   - 未登录时展示登录卡片，点击后跳转飞书授权页；
   - 已登录则显示用户名/头像，并加载与本地版共用的 /static/app.js。
   ===================================================================== */
(() => {
  'use strict';

  const API_BASE = '/api/feishu';
  // 共享界面逻辑据此切换接口前缀，必须在加载 app.js 之前设置
  window.DOC_AGENT_API_BASE = API_BASE;

  const gate = document.getElementById('login-gate');
  const title = document.getElementById('login-title');
  const message = document.getElementById('login-message');
  const loginLink = document.getElementById('login-link');
  const hint = document.getElementById('login-hint');
  const redirectBox = document.getElementById('redirect-box');
  const redirectUri = document.getElementById('redirect-uri');
  const redirectTip = document.getElementById('redirect-tip');
  const copyRedirect = document.getElementById('copy-redirect');
  const userCard = document.getElementById('feishu-user-card');
  const userName = document.getElementById('feishu-user-name');
  const userAvatar = document.getElementById('feishu-user-avatar');
  const userInitial = document.getElementById('feishu-user-initial');

  const params = new URLSearchParams(window.location.search);
  const errorCode = params.get('error');
  const errorText = {
    unconfigured: '尚未配置飞书应用。请在 src/setting.json 的 feishu 配置中填写 app_id 与 app_secret。',
    state_invalid: '登录状态校验失败，请重新发起登录。',
    login_failed: '飞书登录失败，请稍后重试，或联系管理员检查飞书应用配置。',
    access_denied: '你取消了飞书授权。如需使用，请重新点击“使用飞书登录”。',
    redirect_uri_invalid:
      '飞书提示重定向 URL 不合法（20029）：请把下方显示的完整回调地址添加到飞书应用 → 安全设置 → 重定向 URL，' +
      '并确认协议、域名、端口、路径完全一致。',
  }[errorCode];

  function showGate(heading, text) {
    if (heading) title.textContent = heading;
    if (text) message.textContent = text;
    gate.hidden = false;
  }

  function hideGate() {
    gate.hidden = true;
  }

  function renderRedirect(status) {
    const uri = status.redirect_uri || '';
    if (!uri) {
      redirectBox.hidden = true;
      return;
    }
    redirectBox.hidden = false;
    redirectUri.textContent = uri;
    if (status.redirect_uri_configured) {
      redirectTip.textContent =
        '该地址来自 setting.json 的 feishu.redirect_uri。请确认它与飞书应用「安全设置 → 重定向 URL」中的条目完全一致' +
        '（协议、域名、端口、路径都要一致）。';
    } else {
      redirectTip.textContent =
        '当前未固定 feishu.redirect_uri，地址按浏览器访问地址推导。请把上面这条完整地址添加到飞书应用' +
        '「安全设置 → 重定向 URL」；换用 localhost / IP / 端口后需要重新登记。';
    }
    copyRedirect.textContent = '复制';
    copyRedirect.onclick = async () => {
      try {
        await navigator.clipboard.writeText(uri);
        copyRedirect.textContent = '已复制';
        setTimeout(() => { copyRedirect.textContent = '复制'; }, 1800);
      } catch {
        copyRedirect.textContent = '请手动复制';
      }
    };
  }

  function renderUser(user) {
    if (!user) return;
    userCard.hidden = false;
    userName.textContent = user.name || '飞书用户';
    userInitial.textContent = (user.name || '飞').trim().slice(0, 1) || '飞';
    if (user.avatar_url) {
      userAvatar.src = user.avatar_url;
      userAvatar.hidden = false;
      userInitial.hidden = true;
      userAvatar.addEventListener('error', () => {
        userAvatar.hidden = true;
        userInitial.hidden = false;
      });
    } else {
      userAvatar.hidden = true;
      userInitial.hidden = false;
    }
  }

  function loadAgentApp() {
    if (document.getElementById('doc-agent-app-script')) return;
    const script = document.createElement('script');
    script.id = 'doc-agent-app-script';
    // 带版本参数，避免浏览器命中旧版共享脚本（旧版只认 /api 前缀）
    script.src = '/static/app.js?v=feishu-1';
    script.async = true;
    script.onerror = () => showGate('界面加载失败', '前端资源加载失败，请刷新页面重试。');
    document.body.appendChild(script);
  }

  // 共享的 app.js 收到 401 时会回调这里，提示重新登录
  window.DOC_AGENT_ON_UNAUTHORIZED = () => {
    userCard.hidden = true;
    loginLink.hidden = false;
    showGate('登录已失效', '请重新使用飞书账号登录。');
  };

  fetch(`${API_BASE}/auth/status`, { credentials: 'same-origin' })
    .then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    })
    .then((status) => {
      renderRedirect(status);
      if (!status.configured) {
        loginLink.hidden = true;
        hint.hidden = false;
        hint.textContent =
          '管理员配置方法：在 src/setting.json 的 feishu 中填写 app_id、app_secret，' +
          '并把飞书开放平台的重定向地址设为 /api/feishu/auth/callback；修改后重启服务。';
        showGate('飞书应用未配置', '当前服务还没有接入飞书应用，完成配置后即可登录。');
        return;
      }

      if (!status.authenticated) {
        loginLink.hidden = false;
        if (errorText) {
          showGate('飞书登录', errorText);
        } else {
          showGate('使用飞书登录', '登录后只显示属于你的会话与文档，其他成员无法查看。');
        }
        return;
      }

      renderUser(status.user);
      hideGate();
      loadAgentApp();
    })
    .catch(() => {
      loginLink.hidden = true;
      showGate('无法连接服务', '请确认后端 Web 服务已启动，然后刷新页面重试。');
    });
})();
