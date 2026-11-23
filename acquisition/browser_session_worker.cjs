#!/usr/bin/env node
/*
 * acquisition/browser_session_worker.cjs
 *
 * V4 切片 2：真实浏览器「持久会话 + 动态观察」worker（协议 acquisition-browser-session-v0.1）。
 * V4 切片 3：URL 模式强制本地 allowlist 过滤代理 + WS/WebRTC/QUIC 出网加固。
 *
 * 协议：stdin/stdout 各一行一个 JSON，**串行**处理请求。
 *   请求 {"id":N,"command":"...",...}
 *   响应 {"id":N,"ok":true,"result":{...}} 或 {"id":N,"ok":false,"error":"..."}
 *   未知命令返回错误；进程退出前输出 {"closed":true}。
 *
 * 命令：
 *   launch       fixture_mode(bool), allow_url_mode(bool), viewport([w,h]), artifact_dir,
 *                proxy_server(str, URL 模式必填，形如 http://127.0.0.1:<port>)
 *                → {protocol, fixture_mode, allow_url_mode, proxy_server, webrtc_policy,
 *                   browser, executable, node, websocket_route_blocked}
 *   open         url                       → snapshot（仅 URL 模式）
 *   load_fixture html                      → snapshot（仅 fixture 模式）
 *   observe      —                         → snapshot
 *   extract_dom  region_id | "dom:body"    → {region_id, role, bbox, text}
 *   screenshot   region_id | viewport_index→ {file, sha256, bbox, width, height, scroll_y}
 *   interact     region_id, action:"click" → snapshot + changed
 *   navigate     url | html                → snapshot（同一浏览器/页面复用）
 *   close        —                         → {closed:true}，随后退出
 *
 * 一次 launch 复用同一个 Chromium/Edge 浏览器与页面；所有后续命令共享它。
 *
 * 安全边界：
 *   - URL 模式只接受公网 HTTPS：协议/凭据/端口校验 + DNS 全地址公网校验；
 *     每个网络请求再次校验，DNS 按 host 缓存；拦截 media 与超过 200 个请求；
 *     隐藏并暂停 video/audio；页面弹窗一律 dismiss；不加载用户 profile/cookie。
 *   - URL 模式**强制出网隔离**：必须由 driver 提供本地 HTTP CONNECT 过滤代理
 *     （`proxy_server`，只接受 127.0.0.1/::1/localhost），并追加 Chromium 参数
 *     `--proxy-server`、`--proxy-bypass-list=<-loopback>`（loopback 也不绕过）、
 *     `--disable-quic`（禁 UDP/QUIC 直连）、
 *     `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`（WebRTC 不走非代理 UDP）、
 *     `--disable-features=PreconnectToSearch,NetworkPrediction`（禁预连接/网络预测）。
 *     缺 proxy 时 URL 模式直接返回 `egress_proxy_required`，不启动浏览器。
 *   - fixture 模式用固定内部 URL 导航加载 HTML，并拦截**所有** HTTP(S) 请求。
 *   - 两种模式都在页面内禁用 WebSocket/EventSource/RTCPeerConnection/sendBeacon，并优先用
 *     routeWebSocket 关闭连接（进程内纵深防御，不是 OS 级网络隔离）。
 *
 * snapshot 语义：
 *   - role ∈ acquisition/model.py 的 REGION_ROLES；bbox 为页面坐标 [x,y,w,h]；
 *   - 懒加载区域展开前 text=""、lazy=true、requires="click:<按钮文字>"；
 *   - 纯图片/画布区域 text=""（或极短说明文字）、visual_hint=true；
 *   - 验证码/登录墙/访问拒绝整页阻塞写 blocked_reason，区域级登录墙写 requires="login"；
 *   - page_version 是 normalized snapshot（url/title/dom_text/regions）的 SHA-256。
 *
 * 本地 HTML 夹具（供 live-acceptance 使用）的显式标记（优先于启发式推断）：
 *   - data-region-role="activity_rules|comments|poster|..."（值必须是 REGION_ROLES 之一）
 *   - data-region-lazy="true" / data-region-requires="click:展开规则"（或 "login"）
 *   - data-region-blocked="true"
 *   - 整页阻塞：<meta name="acq-blocked" content="captcha|login_wall|access_denied">
 *     或任意元素 data-acq-blocked="captcha|login_wall|access_denied"
 *   无显式标记时按语义标签 + class/id/aria-label 关键词推断 role；<details> 折叠内容、
 *   「展开/查看更多等按钮 + 隐藏内容」会被识别为懒加载区域；页面级验证码/访问拒绝按
 *   标题与正文关键词判定，整页登录墙要求存在密码输入框。
 *
 * 该 worker 不 import agent_v3，安全与 DOM 读取逻辑在本文件独立实现。
 */
'use strict';

const crypto = require('crypto');
const dns = require('dns').promises;
const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');

const PROTOCOL = 'acquisition-browser-session-v0.1';
// 夹具模式通过内部固定 URL 导航加载（route.fulfill），使 addInitScript 与普通导航
// 一致生效；所有其它请求一律 abort。
const FIXTURE_URL = 'https://fixture.local/fixture';
const FIXED_FRAME_COUNT = 3;
const FIXED_FRAME_STEP = 850;
const MAX_REQUESTS = 200;
const MAX_REGIONS = 24;
const REGION_ROLES = Object.freeze([
  'navigation', 'article_body', 'activity_rules', 'comments', 'poster', 'form', 'video',
  'ad', 'login_wall', 'captcha', 'footer', 'section', 'unknown',
]);
const REGION_ROLE_SET = new Set(REGION_ROLES);

const CAPTCHA_RE = /验证码|安全验证|人机验证|captcha|verify you are human/i;
const ACCESS_DENIED_RE = /access denied|forbidden|访问被拒绝|访问受限|无法访问|request blocked/i;
const LOGIN_TEXT_RE = /请先登录|登录后查看|登录后评论|登录后参与|需要登录|sign in to continue|log ?in to continue/i;

let session = null;
let requestChain = Promise.resolve();
let stdinBuffer = '';
let shuttingDown = false;

// --------------------------------------------------------------------------- 基础工具
function writeLine(value) {
  try {
    process.stdout.write(JSON.stringify(value) + '\n');
  } catch (error) {
    /* stdout 已断时无可输出 */
  }
}

function errorMessage(error) {
  if (error && typeof error.message === 'string' && error.message) return error.message.slice(0, 400);
  return String(error).slice(0, 400);
}

function requireReadySession() {
  if (!session) throw new Error('not_launched');
  return session;
}

function isPositiveInt(value) {
  return Number.isInteger(value) && value > 0;
}

function normalizeText(value) {
  return String(value || '')
    .replace(/\u00a0/g, ' ')
    .replace(/\r/g, '')
    .split('\n')
    .map((line) => line.replace(/[ \t]+/g, ' ').trim())
    .filter(Boolean)
    .join('\n');
}

function fixedFrameOffsets(scrollHeight, viewportHeight) {
  const offsets = [];
  for (let index = 0; index < FIXED_FRAME_COUNT; index += 1) {
    const y = Math.min(index * FIXED_FRAME_STEP, Math.max(0, scrollHeight - viewportHeight));
    if (offsets.length && y === offsets[offsets.length - 1]) break;
    offsets.push(y);
  }
  return offsets;
}

function sortedValue(value) {
  if (Array.isArray(value)) return value.map(sortedValue);
  if (value && typeof value === 'object') {
    const result = {};
    Object.keys(value).sort().forEach((key) => {
      result[key] = sortedValue(value[key]);
    });
    return result;
  }
  return value;
}

function pageVersion(snapshotCore) {
  const canonical = JSON.stringify(sortedValue(snapshotCore));
  return 'sha256:' + crypto.createHash('sha256').update(canonical, 'utf8').digest('hex');
}

// --------------------------------------------------------------------------- 公网校验
function isPublicAddress(address) {
  if (net.isIP(address) === 4) {
    const parts = address.split('.').map(Number);
    return !(
      [0, 10, 127].includes(parts[0]) ||
      parts[0] >= 224 ||
      (parts[0] === 169 && parts[1] === 254) ||
      (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31) ||
      (parts[0] === 192 && (parts[1] === 168 || parts[1] === 0 || parts[1] === 2)) ||
      (parts[0] === 100 && parts[1] >= 64 && parts[1] <= 127) ||
      (parts[0] === 198 && [18, 19, 51].includes(parts[1])) ||
      (parts[0] === 203 && parts[1] === 0)
    );
  }
  if (net.isIP(address) === 6) {
    return !/^(::|fc|fd|fe[89ab]|ff|2001:db8)/.test(address.toLowerCase());
  }
  return false;
}

async function assertPublicHttps(rawUrl) {
  let parsed;
  try {
    parsed = new URL(String(rawUrl || ''));
  } catch (error) {
    throw new Error('invalid_url');
  }
  if (parsed.protocol !== 'https:') throw new Error('public_https_required');
  if (parsed.username || parsed.password) throw new Error('public_https_required');
  if (parsed.port && parsed.port !== '443') throw new Error('public_https_required');
  if (!parsed.hostname) throw new Error('invalid_url');
  let addresses;
  try {
    addresses = await dns.lookup(parsed.hostname, { all: true });
  } catch (error) {
    throw new Error('dns_lookup_failed');
  }
  if (!addresses.length || addresses.some((item) => !isPublicAddress(item.address))) {
    throw new Error('non_public_address');
  }
  return parsed.href;
}

async function isPublicHostname(hostname) {
  try {
    const addresses = await dns.lookup(hostname, { all: true });
    return addresses.length > 0 && addresses.every((item) => isPublicAddress(item.address));
  } catch (error) {
    return false;
  }
}

// --------------------------------------------------------------------------- 出网代理
function parseProxyServer(value) {
  /* 出网隔离代理地址校验：只接受本机 loopback 的 HTTP CONNECT 代理，避免配置错误
   * 让 Chromium 直连或改走第三方代理绕过 allowlist。返回规范化后的
   * `http://127.0.0.1:<port>`；非法输入抛 invalid_proxy_server。 */
  if (typeof value !== 'string' || !value.trim()) return null;
  let parsed;
  try {
    parsed = new URL(value.trim());
  } catch (error) {
    throw new Error('invalid_proxy_server');
  }
  if (parsed.protocol !== 'http:') throw new Error('invalid_proxy_server');
  if (parsed.username || parsed.password) throw new Error('invalid_proxy_server');
  if (parsed.pathname && parsed.pathname !== '/') throw new Error('invalid_proxy_server');
  if (parsed.search || parsed.hash) throw new Error('invalid_proxy_server');
  const hostname = parsed.hostname.toLowerCase();
  if (!['127.0.0.1', '::1', 'localhost'].includes(hostname)) {
    throw new Error('invalid_proxy_server');
  }
  if (!parsed.port || !/^\d+$/.test(parsed.port)) throw new Error('invalid_proxy_server');
  const port = Number(parsed.port);
  if (!(port >= 1 && port <= 65535)) throw new Error('invalid_proxy_server');
  return 'http://' + (hostname === '::1' ? '[::1]' : hostname) + ':' + port;
}

function chromiumProxyArgs(proxyServer) {
  /* Chromium 出网强制参数：所有流量走本地代理，禁 QUIC/预连接/网络预测，
   * WebRTC 不允许走非代理 UDP。`<-loopback>` 表示 loopback 也不隐式绕过代理。 */
  return [
    '--proxy-server=' + proxyServer,
    '--proxy-bypass-list=<-loopback>',
    '--disable-quic',
    '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
    '--disable-features=PreconnectToSearch,NetworkPrediction',
  ];
}

// --------------------------------------------------------------------------- 资源探测
function resolvePlaywrightModule() {
  const candidates = [
    process.env.V4_PLAYWRIGHT_MODULE,
    process.env.V3_PLAYWRIGHT_MODULE,
    path.join(os.homedir(), '.cache', 'codex-runtimes', 'codex-primary-runtime',
      'dependencies', 'node', 'node_modules', 'playwright'),
  ];
  for (const candidate of candidates) {
    if (candidate && fs.existsSync(candidate)) return candidate;
  }
  throw new Error('playwright_unavailable');
}

function resolveBrowserExecutable() {
  const candidates = [
    process.env.V4_BROWSER_EXECUTABLE,
    process.env.V3_BROWSER_EXECUTABLE,
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/microsoft-edge',
    '/usr/bin/microsoft-edge-stable',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
  ];
  for (const candidate of candidates) {
    if (candidate && fs.existsSync(candidate)) return candidate;
  }
  throw new Error('browser_unavailable');
}

// --------------------------------------------------------------------------- 请求拦截
async function handleRoute(activeSession, route) {
  const request = route.request();
  try {
    if (activeSession.fixtureMode) {
      // 夹具模式通过内部固定 URL 导航加载，HTTP(S) 其余请求一律拒绝；setContent 不会
      // 触发 addInitScript，因此改用导航加载以获得与真实页面一致的初始化与安全钩子。
      if (activeSession.fixtureHtml &&
          request.url() === FIXTURE_URL && request.resourceType() === 'document') {
        return await route.fulfill({
          status: 200,
          contentType: 'text/html; charset=utf-8',
          body: activeSession.fixtureHtml,
        });
      }
      activeSession.blockedRequests += 1;
      return await route.abort();
    }
    activeSession.requests += 1;
    if (request.resourceType() === 'media' || activeSession.requests > MAX_REQUESTS) {
      activeSession.blockedRequests += 1;
      return await route.abort();
    }
    const url = new URL(request.url());
    if (url.protocol !== 'https:' || url.username || url.password ||
        (url.port && url.port !== '443')) {
      activeSession.blockedRequests += 1;
      return await route.abort();
    }
    if (!activeSession.hostChecks.has(url.hostname)) {
      activeSession.hostChecks.set(url.hostname, isPublicHostname(url.hostname));
    }
    const allowed = await activeSession.hostChecks.get(url.hostname);
    if (!allowed) {
      activeSession.blockedRequests += 1;
      return await route.abort();
    }
    return await route.continue();
  } catch (error) {
    activeSession.blockedRequests += 1;
    try {
      await route.abort();
    } catch (ignored) {
      /* route 已结束 */
    }
  }
}

async function secureLoadedPage(activeSession) {
  const page = activeSession.page;
  try {
    await page.addStyleTag({ content: 'video,audio{visibility:hidden!important}' });
  } catch (error) {
    /* CSP 等导致样式注入失败时忽略 */
  }
  try {
    await page.evaluate(() => {
      document.querySelectorAll('video,audio').forEach((node) => {
        try {
          node.pause();
        } catch (error) {
          /* 忽略 */
        }
      });
    });
  } catch (error) {
    /* 忽略 */
  }
}

// --------------------------------------------------------------------------- DOM 读取
/*
 * collectSnapshot 在页面上下文执行（Playwright 序列化后注入），必须自包含。
 * 返回：{url,title,viewport,scroll_height,dom_text,regions,blocked_marker,has_password_input}
 * regions 中额外的 path / trigger_path 只供 worker 内部点击与截图使用，不进入 snapshot。
 */
function collectSnapshot() {
  const MAX = 24;
  const ROLE_SET = new Set(['navigation', 'article_body', 'activity_rules', 'comments',
    'poster', 'form', 'video', 'ad', 'login_wall', 'captcha', 'footer', 'section', 'unknown']);
  const IGNORED_TEXT_TAGS = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'IFRAME',
    'HEAD', 'META', 'LINK', 'TITLE']);
  const IGNORED_PAGE_TAGS = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'IFRAME',
    'NAV', 'FOOTER', 'HEADER', 'FORM', 'ASIDE']);
  const CANDIDATE_SELECTOR = '[data-region-role],[data-acq-role],nav,header,footer,main,article,' +
    'form,figure,video,details,section,aside,[role="region"]';
  const VISUAL_SELECTOR = 'img,canvas,svg,video,picture';
  const TRIGGER_SELECTOR = 'button,summary,[role="button"],[role="tab"],a[href^="#"],' +
    'a[role="button"],input[type="button"],input[type="submit"],label[for]';
  const LAZY_TRIGGER = /展开|查看更多|查看全部|显示更多|加载更多|点击查看|展开全部|查看规则|查看详情|更多内容|load more|show more|read more|expand|details/i;
  const CAPTCHA = /验证码|安全验证|人机验证|captcha|verify you are human/i;
  const LOGIN_TEXT = /登录后(?:查看|评论|参与|继续)|请先登录|需要登录|登录即可|sign in to continue|log ?in to continue/i;
  const ROLE_HINTS = [
    ['captcha', /captcha|验证码|人机验证|安全验证|verify you are human/i],
    ['login_wall', /login|log-in|signin|sign-in|登录|注册/i],
    ['activity_rules', /rules?|规则|activity|activit|活动|公告|详情|announcement|notice/i],
    ['comments', /comment|评论|回复|reply|replies/i],
    ['poster', /poster|海报|banner|cover|封面|hero|宣传图/i],
    ['ad', /(^|[\s_-])ads?($|[\s_-])|广告|sponsor|promo/i],
    ['navigation', /nav|导航|menu|菜单|breadcrumb|面包屑/i],
    ['footer', /footer|页脚|copyright/i],
    ['form', /(^|[\s_-])form($|[\s_-])|表单|input|survey/i],
    ['video', /video|视频|player/i],
    ['article_body', /article|正文|content|detail|文章|artibody|post[-_]?body/i],
    ['section', /section|card|panel|卡片|区块/i],
  ];

  function normalize(value) {
    return String(value || '')
      .replace(/\u00a0/g, ' ')
      .replace(/\r/g, '')
      .split('\n')
      .map((line) => line.replace(/[ \t]+/g, ' ').trim())
      .filter(Boolean)
      .join('\n');
  }

  function styleOf(node) {
    try {
      return window.getComputedStyle(node);
    } catch (error) {
      return null;
    }
  }

  function isVisible(node) {
    if (!node || typeof node.getBoundingClientRect !== 'function') return false;
    const rect = node.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = styleOf(node);
    if (!style) return false;
    if (style.display === 'none' || style.visibility === 'hidden' ||
        style.visibility === 'collapse') return false;
    return true;
  }

  function rectOf(node) {
    const rect = node.getBoundingClientRect();
    return {
      x: rect.left + (window.scrollX || 0),
      y: rect.top + (window.scrollY || 0),
      width: rect.width,
      height: rect.height,
    };
  }

  function elementPath(node) {
    const parts = [];
    let current = node;
    while (current && current.nodeType === 1 && current !== document.documentElement) {
      if (current.id) {
        let unique = false;
        try {
          unique = document.querySelectorAll('#' + CSS.escape(current.id)).length === 1;
        } catch (error) {
          unique = false;
        }
        if (unique) {
          // 唯一 id 本身就是合法选择器；不能再接祖先前缀，否则会变成非法路径。
          parts.unshift('#' + CSS.escape(current.id));
          return parts.join(' > ');
        }
      }
      const tag = current.tagName.toLowerCase();
      let index = 1;
      let sibling = current.previousElementSibling;
      while (sibling) {
        if (sibling.tagName === current.tagName) index += 1;
        sibling = sibling.previousElementSibling;
      }
      parts.unshift(tag + ':nth-of-type(' + index + ')');
      current = current.parentElement;
    }
    parts.unshift('html');
    return parts.join(' > ');
  }

  function visibleText(root, ignoredTags) {
    const parts = [];
    let visited = 0;
    function walk(node) {
      visited += 1;
      if (visited > 20000) return;
      if (node.nodeType === 3) {
        parts.push(String(node.textContent || '').replace(/\s+/g, ' '));
        return;
      }
      if (node.nodeType !== 1) return;
      const tag = node.tagName;
      if (ignoredTags.has(tag)) return;
      if (node.hidden) return;
      if (node.getAttribute && node.getAttribute('aria-hidden') === 'true') return;
      const style = styleOf(node);
      if (!style) return;
      if (style.display === 'none' || style.visibility === 'hidden' ||
          style.visibility === 'collapse') return;
      if (tag === 'DETAILS' && !node.open) {
        const summary = node.querySelector('summary');
        if (summary) walk(summary);
        return;
      }
      const isBlock = /^(block|flex|grid|table|list-item|flow-root)/.test(style.display);
      const separator = isBlock ? '\n' : ' ';
      parts.push(separator);
      for (let index = 0; index < node.childNodes.length; index += 1) {
        walk(node.childNodes[index]);
      }
      parts.push(separator);
    }
    walk(root);
    const joined = parts.join('').replace(/[ \t]+/g, ' ').replace(/ *\n */g, '\n')
      .replace(/\n{3,}/g, '\n\n');
    return normalize(joined).slice(0, 12000);
  }

  function elementText(node) {
    let text = '';
    try {
      text = node.innerText || '';
    } catch (error) {
      text = node.textContent || '';
    }
    return normalize(text).slice(0, 8000);
  }

  function pageDomText() {
    const selectors = ['main', 'article', '#artibody', '#articleContent',
      '.article-content', '.content-detail', '[role="main"]'];
    let root = null;
    for (const selector of selectors) {
      const node = document.querySelector(selector);
      if (node && isVisible(node) && normalize(node.innerText || '').length >= 20) {
        root = node;
        break;
      }
    }
    return visibleText(root || document.body, IGNORED_PAGE_TAGS);
  }

  function blockedMarker() {
    try {
      const meta = document.querySelector('meta[name="acq-blocked"]');
      if (meta) {
        const content = String(meta.getAttribute('content') || '').trim().toLowerCase();
        if (['captcha', 'login_wall', 'access_denied'].includes(content)) return content;
      }
      const holder = document.querySelector('[data-acq-blocked]');
      if (holder) {
        const attr = String(holder.getAttribute('data-acq-blocked') || '').trim().toLowerCase();
        if (['captcha', 'login_wall', 'access_denied'].includes(attr)) return attr;
      }
    } catch (error) {
      /* 忽略 */
    }
    return null;
  }

  function hintSource(node) {
    const className = typeof node.className === 'string' ? node.className : '';
    return [
      node.id || '',
      className,
      node.getAttribute('aria-label') || '',
      node.getAttribute('data-region-role') || node.getAttribute('data-acq-role') || '',
    ].join(' ');
  }

  function explicitRole(node) {
    const value = String(node.getAttribute('data-region-role') ||
      node.getAttribute('data-acq-role') || '').trim();
    return ROLE_SET.has(value) ? value : null;
  }

  function inferRole(node) {
    const explicit = explicitRole(node);
    if (explicit) return explicit;
    const hint = hintSource(node);
    for (const pair of ROLE_HINTS) {
      if (pair[1].test(hint)) return pair[0];
    }
    const tag = node.tagName.toLowerCase();
    if (tag === 'nav' || tag === 'header') return 'navigation';
    if (tag === 'footer') return 'footer';
    if (tag === 'form') return 'form';
    if (tag === 'video') return 'video';
    if (tag === 'img' || tag === 'canvas' || tag === 'svg' || tag === 'figure') return 'poster';
    if (tag === 'article' || tag === 'main') return 'article_body';
    return 'section';
  }

  function hasLargeVisual(node) {
    function big(item) {
      if (!isVisible(item)) return false;
      const rect = item.getBoundingClientRect();
      return rect.width >= 80 && rect.height >= 60;
    }
    if (['IMG', 'CANVAS', 'SVG', 'VIDEO'].includes(node.tagName)) return big(node);
    const nodes = node.querySelectorAll(VISUAL_SELECTOR);
    for (let index = 0; index < nodes.length; index += 1) {
      if (big(nodes[index])) return true;
    }
    return false;
  }

  function visualCoverage(node) {
    const rect = node.getBoundingClientRect();
    const area = Math.max(1, rect.width * rect.height);
    let best = 0;
    function consider(item) {
      if (!isVisible(item)) return;
      const itemRect = item.getBoundingClientRect();
      const itemArea = itemRect.width * itemRect.height;
      if (itemArea > best) best = itemArea;
    }
    if (['IMG', 'CANVAS', 'SVG', 'VIDEO'].includes(node.tagName)) consider(node);
    const nodes = node.querySelectorAll(VISUAL_SELECTOR);
    for (let index = 0; index < nodes.length; index += 1) consider(nodes[index]);
    return Math.min(1, best / area);
  }

  function findTrigger(node) {
    const nodes = node.querySelectorAll(TRIGGER_SELECTOR);
    let fallback = null;
    for (let index = 0; index < nodes.length; index += 1) {
      const trigger = nodes[index];
      if (!isVisible(trigger)) continue;
      const label = normalize(trigger.innerText || trigger.value ||
        trigger.getAttribute('aria-label') || '').slice(0, 60) || '展开';
      if (trigger.getAttribute('aria-expanded') === 'false' || LAZY_TRIGGER.test(label)) {
        return { node: trigger, label };
      }
      if (!fallback) fallback = { node: trigger, label };
    }
    return fallback;
  }

  function hiddenDescendant(node) {
    if (node.tagName === 'DETAILS' && !node.open) return node;
    let closed = null;
    try {
      closed = node.querySelector('details:not([open])');
    } catch (error) {
      closed = null;
    }
    if (closed) return closed;
    const nodes = node.querySelectorAll('*');
    for (let index = 0; index < nodes.length; index += 1) {
      const child = nodes[index];
      if (IGNORED_TEXT_TAGS.has(child.tagName)) continue;
      if (child.tagName === 'BR' || child.tagName === 'HR') continue;
      if (child.hidden) return child;
      const style = styleOf(child);
      if (!style) continue;
      if (style.display === 'none' || style.visibility === 'hidden' ||
          style.visibility === 'collapse') return child;
    }
    return null;
  }

  function lazyInfo(node, visibleTextLength) {
    const explicitLazy = node.getAttribute('data-region-lazy') || node.getAttribute('data-lazy');
    if (explicitLazy && /^(1|true|yes)$/i.test(String(explicitLazy).trim())) {
      const trigger = findTrigger(node);
      return {
        lazy: true,
        label: (trigger && trigger.label) || '展开',
        trigger_path: trigger ? elementPath(trigger.node) : null,
      };
    }
    const explicitRequires = node.getAttribute('data-region-requires') ||
      node.getAttribute('data-requires');
    if (explicitRequires && /^click:/i.test(String(explicitRequires).trim())) {
      const trigger = findTrigger(node);
      const label = String(explicitRequires).trim().slice(6) || (trigger && trigger.label) || '展开';
      return { lazy: true, label, trigger_path: trigger ? elementPath(trigger.node) : null };
    }
    if (node.tagName === 'DETAILS' && !node.open) {
      const summary = node.querySelector('summary');
      const trigger = summary && isVisible(summary)
        ? { node: summary, label: normalize(summary.innerText || summary.textContent || '').slice(0, 60) || '展开' }
        : findTrigger(node);
      return {
        lazy: true,
        label: (trigger && trigger.label) || '展开',
        trigger_path: trigger ? elementPath(trigger.node) : null,
      };
    }
    const trigger = findTrigger(node);
    if (!trigger) return { lazy: false };
    // 大段正文容器（如整页 main/article）不是懒加载包装器，避免整页被判成懒加载。
    if (visibleTextLength > 200) return { lazy: false };
    if (trigger.node.getAttribute('aria-expanded') === 'false') {
      return { lazy: true, label: trigger.label, trigger_path: elementPath(trigger.node) };
    }
    if (!LAZY_TRIGGER.test(trigger.label)) return { lazy: false };
    const hidden = hiddenDescendant(node);
    let controlsHidden = false;
    const controlsId = trigger.node.getAttribute('aria-controls');
    if (controlsId) {
      try {
        const target = document.getElementById(controlsId);
        if (target) {
          const targetText = normalize(target.textContent || '');
          if (!isVisible(target) || !targetText) controlsHidden = true;
        }
      } catch (error) {
        controlsHidden = false;
      }
    }
    if (hidden || controlsHidden) {
      return { lazy: true, label: trigger.label, trigger_path: elementPath(trigger.node) };
    }
    return { lazy: false };
  }

  const candidates = new Set();
  document.querySelectorAll(CANDIDATE_SELECTOR).forEach((node) => {
    candidates.add(node);
  });
  document.querySelectorAll(VISUAL_SELECTOR).forEach((node) => {
    if (hasLargeVisual(node)) candidates.add(node);
  });

  const ordered = Array.from(candidates)
    .filter((node) => {
      if (!node || node === document.body || node === document.documentElement) return false;
      if (!isVisible(node)) return false;
      const rect = node.getBoundingClientRect();
      return rect.width >= 40 && rect.height >= 20;
    })
    .sort((a, b) => {
      if (a === b) return 0;
      return (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1;
    });

  const regions = [];
  const accepted = [];
  for (let index = 0; index < ordered.length && regions.length < MAX; index += 1) {
    const node = ordered[index];
    const role = inferRole(node);
    let nestedSameRole = false;
    for (let j = 0; j < accepted.length; j += 1) {
      if (accepted[j].role === role && (accepted[j].node === node || accepted[j].node.contains(node))) {
        nestedSameRole = true;
        break;
      }
    }
    if (nestedSameRole) continue;
    const rect = rectOf(node);
    if (rect.width < 1 || rect.height < 1) continue;
    const bbox = [
      Math.max(0, Math.round(rect.x)),
      Math.max(0, Math.round(rect.y)),
      Math.max(1, Math.round(rect.width)),
      Math.max(1, Math.round(rect.height)),
    ];
    const rawText = elementText(node);
    const lazy = lazyInfo(node, rawText.length);
    const text = lazy.lazy ? '' : rawText;
    let finalRole = role;
    if (!explicitRole(node) && finalRole !== 'captcha' && CAPTCHA.test(text) && text.length <= 200) {
      finalRole = 'captcha';
    }
    const explicitRequires = node.getAttribute('data-region-requires') ||
      node.getAttribute('data-requires');
    let requires = null;
    if (lazy.lazy) {
      requires = 'click:' + lazy.label;
    } else if (explicitRequires && /^login$/i.test(String(explicitRequires).trim())) {
      requires = 'login';
    } else if (finalRole === 'login_wall' || LOGIN_TEXT.test(text)) {
      requires = 'login';
    } else if (explicitRequires) {
      requires = String(explicitRequires);
    }
    const explicitBlocked = String(node.getAttribute('data-region-blocked') || '')
      .trim().toLowerCase();
    const blocked = /^(1|true|yes)$/.test(explicitBlocked) || finalRole === 'captcha';
    const visualHint = !lazy.lazy && text.length <= 120 && visualCoverage(node) >= 0.25;
    const region = {
      region_id: 'dom_' + regions.length,
      role: finalRole,
      bbox,
      text,
      visual_hint: visualHint,
      lazy: Boolean(lazy.lazy),
      requires,
      blocked,
      scroll_y: Math.max(0, bbox[1]),
      path: elementPath(node),
      trigger_path: lazy.trigger_path || null,
    };
    regions.push(region);
    accepted.push({ node, role: finalRole, region });
  }

  // 嵌套懒加载容器去重：外层容器（如整页 main/article）若包住另一个已识别的懒加载区域，
  // 它本身是普通容器而不是懒加载包装器，恢复其可见文本，避免策略把整页当成折叠区域。
  for (let outer = 0; outer < accepted.length; outer += 1) {
    const outerEntry = accepted[outer];
    if (!outerEntry.region.lazy) continue;
    for (let inner = 0; inner < accepted.length; inner += 1) {
      if (inner === outer) continue;
      const innerEntry = accepted[inner];
      if (!innerEntry.region.lazy) continue;
      if (outerEntry.node === innerEntry.node) continue;
      if (!outerEntry.node.contains(innerEntry.node)) continue;
      outerEntry.region.lazy = false;
      outerEntry.region.requires = null;
      outerEntry.region.trigger_path = null;
      outerEntry.region.text = elementText(outerEntry.node);
      outerEntry.region.visual_hint = outerEntry.region.text.length <= 120 &&
        visualCoverage(outerEntry.node) >= 0.25;
      break;
    }
  }

  const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 1440;
  const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 1000;
  const scrollHeight = Math.max(
    document.documentElement ? document.documentElement.scrollHeight : 0,
    document.body ? document.body.scrollHeight : 0,
  );

  return {
    url: window.location.href,
    title: document.title || '',
    viewport: [viewportWidth, viewportHeight],
    scroll_height: scrollHeight,
    dom_text: pageDomText(),
    regions,
    blocked_marker: blockedMarker(),
    has_password_input: Boolean(document.querySelector('input[type="password"]')),
  };
}

function detectBlocked(raw, httpStatus) {
  const marker = raw.blocked_marker;
  if (marker === 'captcha' || marker === 'login_wall' || marker === 'access_denied') return marker;
  const title = String(raw.title || '');
  const text = String(raw.dom_text || '');
  const head = title + '\n' + text.slice(0, 1500);
  if (CAPTCHA_RE.test(title) || (CAPTCHA_RE.test(head) && text.length < 2000)) return 'captcha';
  if (httpStatus === 401) return 'login_wall';
  if (httpStatus === 403 || httpStatus === 429) return 'access_denied';
  if (ACCESS_DENIED_RE.test(title) || (ACCESS_DENIED_RE.test(head) && text.length < 1500)) {
    return 'access_denied';
  }
  if (LOGIN_TEXT_RE.test(title)) return 'login_wall';
  if (raw.has_password_input && text.length < 1200) return 'login_wall';
  return null;
}

async function collectPage(activeSession) {
  return await activeSession.page.evaluate(collectSnapshot);
}

async function buildSnapshot(activeSession) {
  const raw = await collectPage(activeSession);
  const url = activeSession.fixtureMode ? 'fixture://local' : String(raw.url || '');
  const regions = raw.regions.map((item) => ({
    region_id: item.region_id,
    role: item.role,
    bbox: item.bbox,
    text: item.text,
    visual_hint: Boolean(item.visual_hint),
    lazy: Boolean(item.lazy),
    requires: item.requires === null || item.requires === undefined ? null : item.requires,
    blocked: Boolean(item.blocked),
    scroll_y: item.scroll_y,
  }));
  const core = {
    url,
    title: String(raw.title || ''),
    dom_text: String(raw.dom_text || ''),
    regions,
  };
  return {
    page_version: pageVersion(core),
    url,
    title: core.title,
    viewport: raw.viewport,
    scroll_height: raw.scroll_height,
    blocked_reason: detectBlocked(raw, activeSession.httpStatus),
    dom_text: core.dom_text,
    regions,
  };
}

// --------------------------------------------------------------------------- 命令实现
async function commandLaunch(request) {
  if (session) throw new Error('already_launched');
  const fixtureMode = request.fixture_mode === true;
  const allowUrlMode = request.allow_url_mode === true;
  // URL 模式默认关闭：初始地址与逐请求校验挡不住重定向/预连接对私网端点的盲连接；
  // 切片 3 起必须显式 allow_url_mode + 本地 allowlist 过滤代理（proxy_server）才允许出网。
  if (!fixtureMode && !allowUrlMode) throw new Error('url_mode_disabled');
  // 出网隔离（切片 3）：URL 模式必须由 driver 注入本地 CONNECT 过滤代理。缺代理时
  // 直接拒绝并且不启动浏览器 —— 宁可不可用，也不允许没有 allowlist 过滤的直连。
  const proxyServer = fixtureMode ? null : parseProxyServer(request.proxy_server);
  if (!fixtureMode && !proxyServer) throw new Error('egress_proxy_required');
  const viewport = Array.isArray(request.viewport) ? request.viewport : [1440, 1000];
  if (viewport.length !== 2 || !isPositiveInt(viewport[0]) || !isPositiveInt(viewport[1])) {
    throw new Error('invalid_viewport');
  }
  let artifactDir = request.artifact_dir;
  if (typeof artifactDir !== 'string' || !artifactDir.trim()) {
    artifactDir = process.env.V4_BROWSER_ARTIFACT_DIR ||
      path.join(process.cwd(), 'data', 'v4', 'acquisition', 'live', 'session');
  }
  artifactDir = path.resolve(artifactDir);
  fs.mkdirSync(artifactDir, { recursive: true });

  const modulePath = resolvePlaywrightModule();
  let playwright;
  try {
    playwright = require(modulePath);
  } catch (error) {
    throw new Error('playwright_load_failed:' + errorMessage(error));
  }
  const executablePath = resolveBrowserExecutable();
  let browser;
  try {
    browser = await playwright.chromium.launch({
      headless: true,
      executablePath,
      // URL 模式强制走本地过滤代理并关闭可能绕过代理的通道；fixture 模式禁网不需要。
      args: fixtureMode ? [] : chromiumProxyArgs(proxyServer),
    });
  } catch (error) {
    throw new Error('browser_launch_failed:' + errorMessage(error));
  }
  const context = await browser.newContext({
    viewport: { width: viewport[0], height: viewport[1] },
    locale: 'zh-CN',
    serviceWorkers: 'block',
    acceptDownloads: false,
    javaScriptEnabled: true,
  });
  // 页面内禁用非 HTTP 出网通道（WebSocket/EventSource/WebRTC/sendBeacon）。
  // URL 与 fixture 模式**都**生效：URL 模式即使已强制代理，也不允许页面另开未代理通道。
  await context.addInitScript(function disableNetworkChannels() {
    const reject = function () { throw new Error('network_channel_disabled'); };
    for (const name of ['WebSocket', 'EventSource', 'RTCPeerConnection',
                        'webkitRTCPeerConnection']) {
      try { Object.defineProperty(window, name, { value: reject, configurable: true }); }
      catch (error) { /* 旧浏览器不支持该通道 */ }
    }
    try { navigator.sendBeacon = function () { return false; }; } catch (error) { /* 忽略 */ }
  });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  page.on('dialog', (dialog) => {
    dialog.dismiss().catch(() => {});
  });
  const activeSession = {
    browser,
    context,
    page,
    fixtureMode,
    allowUrlMode,
    fixtureHtml: null,
    viewport: [viewport[0], viewport[1]],
    artifactDir,
    screenshotIndex: 0,
    requests: 0,
    blockedRequests: 0,
    hostChecks: new Map(),
    httpStatus: null,
    pageReady: false,
    closed: false,
  };
  await context.route('**/*', (route) => handleRoute(activeSession, route));
  // WebSocket 不走 HTTP route；能用 Playwright 的 routeWebSocket 时一律关闭连接。
  // 旧版 Playwright 没有该 API 时由页面内 init script 禁用通道兜底。
  if (typeof context.routeWebSocket === 'function') {
    await context.routeWebSocket('**/*', (socket) => {
      try { socket.close(); } catch (error) { /* 已断开 */ }
    });
    activeSession.websocketRouteBlocked = true;
  } else {
    activeSession.websocketRouteBlocked = false;
  }
  session = activeSession;
  return { protocol: PROTOCOL, fixture_mode: fixtureMode,
           allow_url_mode: allowUrlMode && !fixtureMode,
           proxy_server: fixtureMode ? null : proxyServer,
           webrtc_policy: fixtureMode ? null : 'disable_non_proxied_udp',
           browser: browser.version(),
           executable: executablePath || 'bundled',
           node: process.version,
           websocket_route_blocked: activeSession.websocketRouteBlocked };
}

async function commandOpen(request) {
  const activeSession = requireReadySession();
  if (activeSession.fixtureMode) throw new Error('fixture_mode_network_disabled');
  if (!activeSession.allowUrlMode) throw new Error('url_mode_disabled');
  if (typeof request.url !== 'string' || !request.url) throw new Error('url_required');
  const safeUrl = await assertPublicHttps(request.url);
  const page = activeSession.page;
  let response;
  try {
    response = await page.goto(safeUrl, { waitUntil: 'domcontentloaded', timeout: 25000 });
  } catch (error) {
    activeSession.pageReady = false;
    throw new Error('navigation_failed:' + errorMessage(error));
  }
  activeSession.httpStatus = response ? response.status() : null;
  try {
    await page.waitForTimeout(900);
    await assertPublicHttps(page.url());
  } catch (error) {
    activeSession.pageReady = false;
    try {
      await page.goto('about:blank');
    } catch (ignored) {
      /* 忽略 */
    }
    throw new Error('unsafe_navigation:' + errorMessage(error));
  }
  await secureLoadedPage(activeSession);
  activeSession.pageReady = true;
  return await buildSnapshot(activeSession);
}

async function commandLoadFixture(request) {
  const activeSession = requireReadySession();
  if (!activeSession.fixtureMode) throw new Error('fixture_mode_required');
  if (typeof request.html !== 'string' || !request.html) throw new Error('html_required');
  const page = activeSession.page;
  activeSession.fixtureHtml = request.html;
  try {
    await page.goto(FIXTURE_URL, { waitUntil: 'load', timeout: 15000 });
  } catch (error) {
    activeSession.pageReady = false;
    throw new Error('fixture_load_failed:' + errorMessage(error));
  }
  await page.waitForTimeout(60);
  await secureLoadedPage(activeSession);
  activeSession.httpStatus = null;
  activeSession.pageReady = true;
  return await buildSnapshot(activeSession);
}

async function commandObserve() {
  const activeSession = requireReadySession();
  if (!activeSession.pageReady) throw new Error('page_not_ready');
  return await buildSnapshot(activeSession);
}

async function commandNavigate(request) {
  const activeSession = requireReadySession();
  const hasUrl = typeof request.url === 'string' && request.url.length > 0;
  const hasHtml = typeof request.html === 'string' && request.html.length > 0;
  if (hasUrl && hasHtml) throw new Error('navigate_args_conflict');
  if (hasHtml) {
    if (!activeSession.fixtureMode) throw new Error('fixture_mode_required');
    return await commandLoadFixture({ html: request.html });
  }
  if (hasUrl) {
    if (activeSession.fixtureMode) throw new Error('fixture_mode_network_disabled');
    return await commandOpen({ url: request.url });
  }
  throw new Error('navigate_args_required');
}

async function commandExtractDom(request) {
  const activeSession = requireReadySession();
  if (!activeSession.pageReady) throw new Error('page_not_ready');
  const regionId = String(request.region_id || '');
  if (!regionId) throw new Error('region_id_required');
  const scan = await collectPage(activeSession);
  if (regionId === 'dom:body') {
    const height = Math.max(scan.viewport[1], scan.scroll_height);
    return {
      region_id: regionId,
      role: 'section',
      bbox: [0, 0, scan.viewport[0], height],
      text: String(scan.dom_text || ''),
    };
  }
  const entry = scan.regions.find((item) => item.region_id === regionId);
  if (!entry) throw new Error('unknown_region:' + regionId);
  let text = '';
  if (!entry.lazy) {
    try {
      text = await activeSession.page.locator(entry.path).first().innerText({ timeout: 5000 });
    } catch (error) {
      text = '';
    }
  }
  return {
    region_id: entry.region_id,
    role: entry.role,
    bbox: entry.bbox,
    text: normalizeText(text).slice(0, 8000),
  };
}

async function commandScreenshot(request) {
  const activeSession = requireReadySession();
  if (!activeSession.pageReady) throw new Error('page_not_ready');
  const page = activeSession.page;
  const fileName = 'frame-' + activeSession.screenshotIndex + '.png';
  activeSession.screenshotIndex += 1;
  const filePath = path.join(activeSession.artifactDir, fileName);
  let bbox;
  let width;
  let height;
  let scrollY;

  if (request.viewport_index !== undefined && request.viewport_index !== null) {
    const index = Number(request.viewport_index);
    if (!Number.isInteger(index) || index < 0) throw new Error('invalid_viewport_index');
    const scrollHeight = await page.evaluate(() => Math.max(
      document.documentElement ? document.documentElement.scrollHeight : 0,
      document.body ? document.body.scrollHeight : 0,
    ));
    const offsets = fixedFrameOffsets(scrollHeight, activeSession.viewport[1]);
    if (index >= offsets.length) throw new Error('viewport_index_out_of_range');
    const y = offsets[index];
    await page.evaluate((value) => window.scrollTo(0, value), y);
    await page.waitForTimeout(120);
    await page.screenshot({ path: filePath, fullPage: false, animations: 'disabled' });
    const current = await page.evaluate(() => ({ y: window.scrollY || 0 }));
    scrollY = current.y;
    bbox = [0, scrollY, activeSession.viewport[0], activeSession.viewport[1]];
    width = activeSession.viewport[0];
    height = activeSession.viewport[1];
  } else {
    const regionId = String(request.region_id || '');
    if (!regionId) throw new Error('region_id_required');
    if (regionId === 'dom:body') throw new Error('region_screenshot_unsupported:dom:body');
    const scan = await collectPage(activeSession);
    const entry = scan.regions.find((item) => item.region_id === regionId);
    if (!entry) throw new Error('unknown_region:' + regionId);
    const locator = page.locator(entry.path).first();
    const beforeScroll = await page.evaluate(() => window.scrollY || 0);
    try {
      await locator.scrollIntoViewIfNeeded({ timeout: 5000 });
    } catch (error) {
      /* 不可滚动时按当前位置截取 */
    }
    await page.waitForTimeout(80);
    const metrics = await locator.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return {
        x: rect.left + (window.scrollX || 0),
        y: rect.top + (window.scrollY || 0),
        width: rect.width,
        height: rect.height,
      };
    });
    if (!(metrics.width >= 1 && metrics.height >= 1)) throw new Error('region_not_rendered');
    await locator.screenshot({ path: filePath, animations: 'disabled' });
    await page.evaluate((value) => window.scrollTo(0, value), beforeScroll).catch(() => {});
    const regionX = Math.max(0, Math.round(metrics.x));
    const regionY = Math.max(0, Math.round(metrics.y));
    width = Math.max(1, Math.round(metrics.width));
    height = Math.max(1, Math.round(metrics.height));
    bbox = [regionX, regionY, width, height];
    scrollY = regionY;
  }

  const sha256 = crypto.createHash('sha256').update(fs.readFileSync(filePath)).digest('hex');
  return { file: filePath, sha256, bbox, width, height, scroll_y: scrollY };
}

async function commandInteract(request) {
  const activeSession = requireReadySession();
  if (!activeSession.pageReady) throw new Error('page_not_ready');
  const regionId = String(request.region_id || '');
  if (!regionId) throw new Error('region_id_required');
  const page = activeSession.page;
  const before = await buildSnapshot(activeSession);
  const scan = await collectPage(activeSession);
  const entry = scan.regions.find((item) => item.region_id === regionId);
  if (!entry) throw new Error('unknown_region:' + regionId);
  const clickPath = entry.trigger_path || entry.path;
  if (!clickPath) throw new Error('click_no_target');
  const locator = page.locator(clickPath).first();
  const count = await locator.count();
  if (!count) throw new Error('region_detached');
  try {
    await locator.scrollIntoViewIfNeeded({ timeout: 5000 });
  } catch (error) {
    /* 不可滚动时直接点击 */
  }
  try {
    await locator.click({ timeout: 5000 });
  } catch (error) {
    throw new Error('click_failed:' + errorMessage(error));
  }
  await page.waitForTimeout(120);
  let after = await buildSnapshot(activeSession);
  for (let attempt = 0; attempt < 4 && after.page_version === before.page_version; attempt += 1) {
    await page.waitForTimeout(180);
    after = await buildSnapshot(activeSession);
  }
  after.changed = after.page_version !== before.page_version;
  return after;
}

async function teardownSession() {
  if (!session) return;
  const active = session;
  session = null;
  active.closed = true;
  try {
    await active.page.close();
  } catch (error) {
    /* 忽略 */
  }
  try {
    await active.context.close();
  } catch (error) {
    /* 忽略 */
  }
  try {
    await active.browser.close();
  } catch (error) {
    /* 忽略 */
  }
}

async function commandClose() {
  await teardownSession();
  return { closed: true };
}

// --------------------------------------------------------------------------- 主循环
const COMMANDS = {
  launch: commandLaunch,
  open: commandOpen,
  load_fixture: commandLoadFixture,
  observe: commandObserve,
  extract_dom: commandExtractDom,
  screenshot: commandScreenshot,
  interact: commandInteract,
  navigate: commandNavigate,
  close: commandClose,
};

async function dispatch(request) {
  const command = String(request.command || '');
  const handler = COMMANDS[command];
  if (!handler) throw new Error('unknown_command:' + (command || '<empty>'));
  return await handler(request);
}

async function finish(code) {
  if (shuttingDown) return;
  shuttingDown = true;
  try {
    await teardownSession();
  } catch (error) {
    /* 忽略 */
  }
  writeLine({ closed: true });
  process.exitCode = code;
  setTimeout(() => process.exit(code), 50);
}

function handleLine(line) {
  let request;
  try {
    request = JSON.parse(line);
  } catch (error) {
    writeLine({ id: null, ok: false, error: 'invalid_json' });
    return Promise.resolve();
  }
  if (!request || typeof request !== 'object' || Array.isArray(request)) {
    writeLine({ id: null, ok: false, error: 'invalid_request' });
    return Promise.resolve();
  }
  const id = Object.prototype.hasOwnProperty.call(request, 'id') ? request.id : null;
  const command = String(request.command || '');
  return (async () => {
    let result;
    try {
      result = await dispatch(request);
    } catch (error) {
      writeLine({ id, ok: false, error: errorMessage(error) });
      if (command === 'close') await finish(0);
      return;
    }
    writeLine({ id, ok: true, result });
    if (command === 'close') await finish(0);
  })();
}

process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  stdinBuffer += chunk;
  const lines = stdinBuffer.split('\n');
  stdinBuffer = lines.pop();
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    requestChain = requestChain.then(() => handleLine(trimmed)).catch(() => {});
  }
});
process.stdin.on('end', () => {
  requestChain = requestChain.then(() => finish(0)).catch(() => {});
});
process.stdout.on('error', () => {
  try {
    process.exit(1);
  } catch (error) {
    /* 忽略 */
  }
});
process.on('uncaughtException', (error) => {
  writeLine({ id: null, ok: false, error: 'fatal:' + errorMessage(error) });
  process.exit(1);
});
process.on('unhandledRejection', (error) => {
  writeLine({ id: null, ok: false, error: 'fatal:' + errorMessage(error) });
  process.exit(1);
});
