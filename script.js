// ============ ELEMENTS ============
const $ = (id) => document.getElementById(id);
const root = document.documentElement;
const center = $('center');
const input = $('input');
const thread = $('thread');
const themeBtn = $('themeBtn');
const newThreadBtn = $('newThread');
const sendBtn = $('send');
const greetEl = $('greet');
const subEl = $('sub');
const welcomeEl = $('welcome');
const meter = $('tokenMeter');
const tokLeft = $('tokLeft');
const tokMax = $('tokMax');
const tokBar = $('tokBar');

const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const isMobile = () => window.matchMedia('(pointer: coarse)').matches || window.innerWidth <= 768;
const GUEST_MESSAGE_LIMIT = 3;

let threadId = null;
let busy = false;

// ============ AUTH STATE ============
let authToken = localStorage.getItem('athena_token') || '';
let currentUser = (() => {
  try { return JSON.parse(localStorage.getItem('athena_user') || 'null'); } catch (e) { return null; }
})();

function authHeaders(extra = {}) {
  const h = { ...extra };
  if (authToken) h['Authorization'] = 'Bearer ' + authToken;
  return h;
}

function getDisplayName() {
  return (currentUser && currentUser.name) ? currentUser.name : 'Athena';
}

function updateUserUI() {
  const avatar = $('userAvatar');
  if (!avatar) return;
  if (currentUser) {
    avatar.textContent = (currentUser.name || currentUser.email || 'U').charAt(0).toUpperCase();
    avatar.title = `${currentUser.name} (${currentUser.email})`;
  } else {
    avatar.textContent = '?';
    avatar.title = 'Klik untuk Masuk / Daftar';
  }
}

// ID anonim cadangan jika user belum login
function getClientId() {
  try {
    let id = localStorage.getItem('clientId');
    if (!id) {
      const raw = (window.crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : Date.now() + Math.random().toString(36).slice(2);
      id = String(raw).replace(/[^A-Za-z0-9_-]/g, '');
      localStorage.setItem('clientId', id);
    }
    return id;
  } catch (e) {
    return 'anon' + Math.random().toString(36).slice(2);
  }
}
const CLIENT_ID = getClientId();

const fmt = (n) => new Intl.NumberFormat('id-ID').format(n);

// ============ THEME ============

function setTheme(theme) {
  root.setAttribute('data-theme', theme);
  try { localStorage.setItem('theme', theme); } catch (e) {}
}

(function loadTheme() {
  try {
    const saved = localStorage.getItem('theme');
    if (saved) root.setAttribute('data-theme', saved);
  } catch (e) {}
})();

themeBtn.onclick = () =>
  setTheme(root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');

// ============ GREETING / WELCOME ============

const isReturning = (() => {
  try {
    const seen = localStorage.getItem('visited');
    localStorage.setItem('visited', '1');
    return !!seen;
  } catch (e) { return false; }
})();

function greetingWord() {
  const h = new Date().getHours();
  if (h < 4) return 'Selamat malam';
  if (h < 11) return 'Selamat pagi';
  if (h < 15) return 'Selamat siang';
  if (h < 19) return 'Selamat sore';
  return 'Selamat malam';
}

let heroRun = 0;

function typeInto(el, text, run, speed = 42) {
  return new Promise((resolve) => {
    if (reduceMotion) { el.textContent = text; return resolve(); }
    el.textContent = '';
    el.classList.add('caret');
    let i = 0;
    const timer = setInterval(() => {
      if (run !== heroRun) { clearInterval(timer); el.classList.remove('caret'); return resolve(); }
      i++;
      el.textContent = text.slice(0, i);
      if (i >= text.length) {
        clearInterval(timer);
        el.classList.remove('caret');
        resolve();
      }
    }, speed);
  });
}

async function playHero() {
  const run = ++heroRun;
  welcomeEl.textContent = isReturning ? 'Selamat datang kembali' : 'Selamat datang di Athena';
  welcomeEl.classList.remove('show');
  void welcomeEl.offsetWidth;
  welcomeEl.classList.add('show');
  greetEl.textContent = '';
  subEl.textContent = '';
  await typeInto(greetEl, `${greetingWord()}, ${getDisplayName()}`, run);
  if (run !== heroRun) return;
  await typeInto(subEl, 'Apa yang sedang kamu pikirkan?', run);
}

// ============ MENUS ============

function toggleMenu(menuId) {
  const menu = $(menuId);
  if (!menu) return;
  const isOpen = menu.classList.contains('open');
  document.querySelectorAll('.menu').forEach((m) => m.classList.remove('open'));
  if (!isOpen) menu.classList.add('open');
}
window.toggleMenu = toggleMenu;

document.addEventListener('click', (e) => {
  if (!e.target.closest('.menu')) {
    document.querySelectorAll('.menu').forEach((m) => m.classList.remove('open'));
  }
});

document.querySelectorAll('.menu[data-pick] li').forEach((li) => {
  li.onclick = () => {
    const menu = li.closest('.menu');
    menu.querySelector('.pick-label').textContent = li.textContent;
    menu.dataset.value = li.textContent;
    menu.classList.remove('open');
  };
});

const currentModel = () => 'Gemini';
const currentStyle = () => $('styleMenu').dataset.value || 'Casual';

// ============ EXAMPLE CARDS ============

document.querySelectorAll('.card').forEach((card) => {
  card.onclick = () => {
    input.value = card.dataset.p;
    autosize();
    input.focus();
  };
});

// ============ TEXTAREA ============

function autosize() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 200) + 'px';
}
input.addEventListener('input', autosize);

// ============ TOKEN METER ============

function updateTokens(u) {
  if (!u) return;
  tokLeft.textContent = fmt(u.remaining);
  tokMax.textContent = fmt(u.limit);
  const pct = u.limit ? Math.max(0, Math.min(100, (u.remaining / u.limit) * 100)) : 0;
  tokBar.style.width = pct + '%';
  meter.classList.toggle('empty', u.remaining <= 0);
  meter.classList.toggle('low', u.remaining > 0 && pct <= 20);
  meter.classList.remove('pop');
  void meter.offsetWidth;
  meter.classList.add('pop');
}

async function loadUsage() {
  try {
    const res = await fetch('/api/usage?client_id=' + encodeURIComponent(CLIENT_ID), {
      headers: authHeaders(),
    });
    const json = await res.json();
    updateTokens(json.data && json.data.usage);
  } catch (e) {
    tokLeft.textContent = '–';
    tokMax.textContent = '–';
  }
}

// ============ MARKDOWN ============

function renderMarkdown(src) {
  const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const inline = (t) => esc(t)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');

  let html = '';
  let inCode = false;
  let code = [];
  let list = null;
  let para = [];

  const flushPara = () => {
    if (para.length) { html += '<p>' + para.map(inline).join('<br>') + '</p>'; para = []; }
  };
  const flushList = () => {
    if (list) {
      html += `<${list.tag}>` + list.items.map((i) => '<li>' + inline(i) + '</li>').join('') + `</${list.tag}>`;
      list = null;
    }
  };
  const pushItem = (tag, text) => {
    flushPara();
    if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
    list.items.push(text);
  };

  for (const line of src.split('\n')) {
    if (/^```/.test(line)) {
      if (inCode) {
        html += '<pre><code>' + esc(code.join('\n')) + '</code></pre>';
        code = []; inCode = false;
      } else {
        flushPara(); flushList(); inCode = true;
      }
      continue;
    }
    if (inCode) { code.push(line); continue; }

    let m;
    if (!line.trim()) { flushPara(); flushList(); continue; }
    if ((m = line.match(/^#{1,4}\s+(.*)/))) { flushPara(); flushList(); html += '<h4>' + inline(m[1]) + '</h4>'; continue; }
    if ((m = line.match(/^\s*[-*•]\s+(.*)/))) { pushItem('ul', m[1]); continue; }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { pushItem('ol', m[1]); continue; }
    flushList();
    para.push(line);
  }
  if (inCode) html += '<pre><code>' + esc(code.join('\n')) + '</code></pre>';
  flushPara();
  flushList();
  return html;
}

// ============ SCROLL ============

const nearBottom = () => center.scrollHeight - center.scrollTop - center.clientHeight < 140;

function scrollDown(smooth) {
  center.scrollTo({ top: center.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
}

// ============ MESSAGES ============

function setChatting(on) {
  center.classList.toggle('chatting', on);
}

function addUserMessage(text, scroll = true) {
  const div = document.createElement('div');
  div.className = 'msg user';
  div.textContent = text || '';
  thread.appendChild(div);
  if (scroll) scrollDown(true);
}

function addError(text) {
  const div = document.createElement('div');
  div.className = 'msg err';
  div.textContent = text;
  thread.appendChild(div);
  scrollDown(true);
}

function showTyping() {
  const div = document.createElement('div');
  div.className = 'msg typing-indicator';
  div.innerHTML = '<span class="dots"><i></i><i></i><i></i></span><span class="t-label">Athena sedang mengetik…</span>';
  thread.appendChild(div);
  scrollDown(true);
  return div;
}

function typewriter(bubble, body, text) {
  return new Promise((resolve) => {
    if (reduceMotion) { body.innerHTML = renderMarkdown(text); return resolve(); }
    const total = text.length;
    const duration = Math.min(Math.max(total * 14, 700), 5500);
    const start = performance.now();
    bubble.classList.add('typing');

    function frame(now) {
      const p = Math.min(1, (now - start) / duration);
      const stick = nearBottom();
      body.innerHTML = renderMarkdown(text.slice(0, Math.floor(total * p)));
      if (stick) center.scrollTop = center.scrollHeight;
      if (p < 1) {
        requestAnimationFrame(frame);
      } else {
        body.innerHTML = renderMarkdown(text);
        bubble.classList.remove('typing');
        resolve();
      }
    }
    requestAnimationFrame(frame);
  });
}

function addFooter(bubble, text, metaText) {
  const foot = document.createElement('div');
  foot.className = 'msg-foot';
  const small = document.createElement('small');
  small.textContent = metaText;
  const copy = document.createElement('button');
  copy.className = 'copy';
  copy.textContent = 'Salin';
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(text);
      copy.textContent = 'Tersalin ✓';
      setTimeout(() => (copy.textContent = 'Salin'), 1500);
    } catch (e) {}
  };
  foot.append(small, copy);
  bubble.appendChild(foot);
}

function createAIBubble() {
  const bubble = document.createElement('div');
  bubble.className = 'msg ai';
  const body = document.createElement('div');
  body.className = 'md';
  bubble.appendChild(body);
  thread.appendChild(bubble);
  return { bubble, body };
}

async function showAIReply(text, metaText) {
  const { bubble, body } = createAIBubble();
  await typewriter(bubble, body, text);
  addFooter(bubble, text, metaText);
  if (nearBottom()) scrollDown(true);
}

function restoreAIReply(m) {
  const { bubble, body } = createAIBubble();
  body.innerHTML = renderMarkdown(m.content);
  const meta = (m.tokens ? `⚡ ${fmt(m.tokens)} token · ` : '') + (m.model || '');
  addFooter(bubble, m.content, meta);
}

// ============ FOLLOW-UP SUGGESTIONS ============

const FALLBACK_SUGGESTIONS = ['Jelaskan lebih detail', 'Berikan contoh', 'Ringkas jawabanmu'];

function clearSuggestions() {
  thread.querySelectorAll('.suggest').forEach((el) => el.remove());
}

function showSuggestions(list) {
  const items = (list && list.length ? list : FALLBACK_SUGGESTIONS).slice(0, 3);
  const box = document.createElement('div');
  box.className = 'suggest';
  items.forEach((t, i) => {
    const b = document.createElement('button');
    b.textContent = t;
    b.style.animationDelay = i * 90 + 'ms';
    b.onclick = () => submitMessage(t);
    box.appendChild(b);
  });
  thread.appendChild(box);
  if (nearBottom()) scrollDown(true);
}

// ============ SEND ============

async function submitMessage(textOverride) {
  if (busy) return;
  const value = (typeof textOverride === 'string' ? textOverride : input.value).trim();
  if (!value) return;

  // Batasi maksimal 3 pesan untuk pengguna tamu (belum login)
  if (!currentUser) {
    const guestCount = Number(localStorage.getItem('athena_guest_count') || '0');
    if (guestCount >= GUEST_MESSAGE_LIMIT) {
      const limitMsg = `Batas ${GUEST_MESSAGE_LIMIT} pesan gratis untuk tamu sudah habis. Silakan masuk atau daftar dulu.`;
      if (window.openAuthModal) {
        window.openAuthModal(limitMsg);
      } else {
        alert(limitMsg);
      }
      return;
    }
  }

  busy = true;
  sendBtn.disabled = true;

  setChatting(true);
  clearSuggestions();
  addUserMessage(value);

  input.value = '';
  autosize();

  const typing = showTyping();
  const result = await callAIAPI(value);
  typing.remove();

  if (result.usage) updateTokens(result.usage);

  if (!result.ok) {
    addError(result.error);
    if (result.requiresAuth && window.openAuthModal) {
      window.openAuthModal(result.error);
    }
  } else {
    if (!currentUser) {
      const nextCount = Number(localStorage.getItem('athena_guest_count') || '0') + 1;
      localStorage.setItem('athena_guest_count', String(nextCount));
    }
    const d = result.data;
    if (d.thread_id) threadId = d.thread_id;
    loadHistory();
    await showAIReply(d.response, `⚡ ${fmt(d.tokens_used)} token · ${d.model}`);
    showSuggestions(d.suggestions);
  }

  busy = false;
  sendBtn.disabled = false;
  input.focus();
}

sendBtn.onclick = () => submitMessage();

// Di desktop: Enter = kirim, Shift+Enter = baris baru. Di HP (mobile): Enter = baris baru.
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !isMobile()) {
    e.preventDefault();
    submitMessage();
  }
});

// ============ NEW THREAD ============

function startNewThread() {
  thread.innerHTML = '';
  threadId = null;
  setChatting(false);
  input.value = '';
  autosize();
  center.scrollTop = 0;
  input.focus();
  playHero();
  renderHistory();
}

newThreadBtn.onclick = () => {
  if (busy) return;
  startNewThread();
};

// ============ API ============

async function callAIAPI(message) {
  try {
    const form = new FormData();
    form.append('message', message);
    form.append('model', currentModel());
    form.append('style', currentStyle());
    form.append('client_id', CLIENT_ID);
    if (threadId) form.append('thread_id', threadId);

    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: authHeaders(),
      body: form,
    });
    const json = await response.json();

    const usage = json.data && json.data.usage;
    if (json.status !== 'success') {
      return {
        ok: false,
        error: json.error || 'Terjadi kesalahan',
        usage,
        requiresAuth: !!(json.data && json.data.requires_auth),
      };
    }
    return { ok: true, data: json.data, usage };
  } catch (error) {
    console.error('Error calling API:', error);
    return { ok: false, error: 'Tidak bisa terhubung ke server: ' + error.message };
  }
}

// ============ CHAT HISTORY ============

const drawer = $('drawer');
const drawerBackdrop = $('drawerBackdrop');
const historyBtn = $('historyBtn');
const historyList = $('historyList');
let historyCache = [];

function openDrawer() {
  drawer.classList.add('open');
  drawerBackdrop.classList.add('open');
  loadHistory();
}

function closeDrawer() {
  drawer.classList.remove('open');
  drawerBackdrop.classList.remove('open');
}

historyBtn.onclick = () => (drawer.classList.contains('open') ? closeDrawer() : openDrawer());
drawerBackdrop.onclick = closeDrawer;
$('drawerClose').onclick = closeDrawer;
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

function dayGroup(iso) {
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((startOf(new Date()) - startOf(new Date(iso))) / 86400000);
  if (diff <= 0) return 'Hari ini';
  if (diff === 1) return 'Kemarin';
  if (diff <= 7) return '7 hari terakhir';
  return 'Lebih lama';
}

async function loadHistory() {
  try {
    const res = await fetch('/api/threads?client_id=' + encodeURIComponent(CLIENT_ID), {
      headers: authHeaders(),
    });
    const json = await res.json();
    historyCache = (json.data && json.data.threads) || [];
    renderHistory();
  } catch (e) {
    console.error('Gagal memuat riwayat:', e);
  }
}

function renderHistory() {
  historyList.innerHTML = '';
  if (!historyCache.length) {
    const p = document.createElement('p');
    p.className = 'hist-empty';
    p.textContent = 'Belum ada riwayat. Mulai ngobrol dulu!';
    historyList.appendChild(p);
    return;
  }

  let lastGroup = '';
  historyCache.forEach((t) => {
    const group = dayGroup(t.last_updated);
    if (group !== lastGroup) {
      const h = document.createElement('div');
      h.className = 'hist-group';
      h.textContent = group;
      historyList.appendChild(h);
      lastGroup = group;
    }

    const item = document.createElement('div');
    item.className = 'hist-item' + (t.id === threadId ? ' active' : '');
    item.tabIndex = 0;
    item.setAttribute('role', 'button');

    const title = document.createElement('span');
    title.className = 'hist-title';
    title.textContent = t.title;

    const del = document.createElement('button');
    del.className = 'hist-del';
    del.textContent = '×';
    del.setAttribute('aria-label', 'Hapus chat: ' + t.title);
    del.onclick = (e) => { e.stopPropagation(); deleteThread(t.id, t.title); };

    item.onclick = () => openThread(t.id);
    item.onkeydown = (e) => {
      if (e.key === 'Enter') { e.preventDefault(); openThread(t.id); }
    };
    item.append(title, del);
    historyList.appendChild(item);
  });
}

async function openThread(id) {
  if (busy) return;
  if (id === threadId) { closeDrawer(); return; }

  try {
    const res = await fetch(`/api/threads/${encodeURIComponent(id)}?client_id=${encodeURIComponent(CLIENT_ID)}`, {
      headers: authHeaders(),
    });
    const json = await res.json();
    if (json.status !== 'success') {
      alert(json.error || 'Riwayat tidak ditemukan.');
      loadHistory();
      return;
    }

    heroRun++;
    thread.innerHTML = '';
    threadId = id;
    setChatting(true);

    const msgs = json.data.messages || [];
    msgs.forEach((m) => {
      if (m.role === 'user') addUserMessage(m.content, false);
      else restoreAIReply(m);
    });

    const last = msgs[msgs.length - 1];
    if (last && last.role === 'assistant') showSuggestions(last.suggestions);

    closeDrawer();
    renderHistory();
    requestAnimationFrame(() => { center.scrollTop = center.scrollHeight; });
  } catch (e) {
    alert('Gagal membuka riwayat: ' + e.message);
  }
}

async function deleteThread(id, title) {
  if (busy && id === threadId) {
    alert('Tunggu Athena selesai menjawab dulu.');
    return;
  }
  if (!confirm(`Hapus chat "${title}"?`)) return;
  try {
    await fetch(`/api/threads/${encodeURIComponent(id)}?client_id=${encodeURIComponent(CLIENT_ID)}`, {
      method: 'DELETE',
      headers: authHeaders(),
    });
  } catch (e) {}
  if (id === threadId) startNewThread();
  loadHistory();
}

// ============ AUTH MODAL LOGIC ============

(function initAuth() {
  const modal = $('authModal');
  const avatarBtn = $('userAvatar');
  const closeBtn = $('authCloseBtn');
  const form = $('authForm');
  const switchBtn = $('authSwitchBtn');
  const switchText = $('authSwitchText');
  const titleEl = $('authModalTitle');
  const nameInput = $('authName');
  const submitBtn = $('authSubmitBtn');
  const errEl = $('authError');
  const outView = $('authLoggedOutView');
  const inView = $('authLoggedInView');
  const logoutBtn = $('logoutBtn');
  if (!modal || !avatarBtn) return;

  let isRegister = false;

  function syncModalView() {
    errEl.style.display = 'none';
    if (currentUser) {
      titleEl.textContent = 'Profil Pengguna';
      outView.style.display = 'none';
      inView.style.display = 'block';
      $('profileName').textContent = currentUser.name;
      $('profileEmail').textContent = currentUser.email;
    } else {
      titleEl.textContent = isRegister ? 'Daftar Akun Athena' : 'Masuk ke Athena';
      outView.style.display = 'block';
      inView.style.display = 'none';
      nameInput.style.display = isRegister ? 'block' : 'none';
      nameInput.required = isRegister;
      submitBtn.textContent = isRegister ? 'Daftar' : 'Masuk';
      switchText.textContent = isRegister ? 'Sudah punya akun?' : 'Belum punya akun?';
      switchBtn.textContent = isRegister ? 'Masuk di sini' : 'Daftar sekarang';
    }
  }

  avatarBtn.onclick = () => {
    syncModalView();
    modal.classList.add('show');
  };

  window.openAuthModal = (msg) => {
    isRegister = false;
    syncModalView();
    if (msg) {
      errEl.textContent = msg;
      errEl.style.display = 'block';
    }
    modal.classList.add('show');
  };

  closeBtn.onclick = () => modal.classList.remove('show');
  modal.onclick = (e) => { if (e.target === modal) modal.classList.remove('show'); };

  switchBtn.onclick = () => {
    isRegister = !isRegister;
    syncModalView();
  };

  form.onsubmit = async (e) => {
    e.preventDefault();
    errEl.style.display = 'none';
    submitBtn.disabled = true;

    const endpoint = isRegister ? '/api/auth/register' : '/api/auth/login';
    const payload = {
      email: $('authEmail').value.trim(),
      password: $('authPassword').value,
      ...(isRegister ? { name: nameInput.value.trim() } : {}),
    };

    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const json = await res.json();

      if (json.status !== 'success') {
        errEl.textContent = json.error || 'Autentikasi gagal.';
        errEl.style.display = 'block';
      } else if (json.data.needs_verification) {
        alert(json.data.message);
        isRegister = false;
        syncModalView();
      } else {
        authToken = json.data.access_token;
        currentUser = json.data.user;
        localStorage.setItem('athena_token', authToken);
        localStorage.setItem('athena_user', JSON.stringify(currentUser));
        updateUserUI();
        modal.classList.remove('show');
        startNewThread();
        loadUsage();
        loadHistory();
      }
    } catch (err) {
      errEl.textContent = 'Gagal terhubung ke server.';
      errEl.style.display = 'block';
    } finally {
