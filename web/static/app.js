let currentState = null;
let currentHudTab = "scene";
let selectedExcerpt = "";
let currentInspectedPack = null;
let currentInspectedDocKey = "";
let hasPromptedOnboardingOnce = false;
let currentGenesisStep = 1;
let latestSelfCheck = null;
let currentObDriverType = "api";
let currentCornerOrnament = "◢";
let pixelAnimTimer = null;
let pixelAnimTick = 0;
let stageResizeObserver = null;
const moodLockMemory = {};
let commandReplies = []; // /recap、/gm 这类命令的回复不算回合，只在当前页面保留到下一回合
let activeTurnAbortCtrl = null;
let activeGenesisAbortCtrl = null;
let toastHideTimer = null;

function renderInlineMd(text) {
  return escapeHtml(text)
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/(^|[\s（(])_([^_\n]+)_(?=$|[\s）),，。])/g, "$1<em>$2</em>");
}

// 面板文本的轻量 Markdown：标题、无序列表、粗体、行内代码、斜体（内容先转义，不允许原始 HTML）
function renderMdLite(source) {
  const out = [];
  let list = [];
  let para = [];
  const flushList = () => {
    if (list.length) out.push(`<ul>${list.map((li) => `<li>${renderInlineMd(li)}</li>`).join("")}</ul>`);
    list = [];
  };
  const flushPara = () => {
    if (para.length) out.push(`<p>${para.map(renderInlineMd).join("<br/>")}</p>`);
    para = [];
  };
  String(source || "")
    .split(/\r?\n/)
    .forEach((raw) => {
      const line = raw.trim();
      const heading = line.match(/^#{1,6}\s+(.+)$/);
      const item = line.match(/^[-*]\s+(.+)$/);
      if (!line) {
        flushList();
        flushPara();
      } else if (heading) {
        flushList();
        flushPara();
        out.push(`<h5>${renderInlineMd(heading[1])}</h5>`);
      } else if (item) {
        flushPara();
        list.push(item[1]);
      } else {
        flushList();
        para.push(line);
      }
    });
  flushList();
  flushPara();
  return out.join("");
}

function hideToast() {
  const el = document.getElementById("nonblocking-toast");
  if (el) el.style.display = "none";
}

function showToast(msg, durationMs = 3200, kind = "") {
  const el = document.getElementById("nonblocking-toast");
  if (!el) return;
  el.textContent = msg;
  el.className = `nonblocking-toast ${kind}`.trim();
  el.style.display = "none";
  void el.offsetWidth; // 重新触发入场动画
  el.style.display = "block";
  if (toastHideTimer) clearTimeout(toastHideTimer);
  toastHideTimer = setTimeout(hideToast, durationMs);
}

function showError(msg) {
  showToast(msg, 9000, "error");
}

const CLIENT_API_PRESETS = {
  deepseek: { api_base: "https://api.deepseek.com/v1", model: "deepseek-chat", api_format: "openai", back_model: "deepseek-reasoner" },
  openai: { api_base: "https://api.openai.com/v1", model: "gpt-4.1", api_format: "openai", back_model: "o3" },
  anthropic: { api_base: "https://api.anthropic.com/v1", model: "claude-sonnet-5-5", api_format: "anthropic", back_model: "claude-opus-5-5" },
  gemini: { api_base: "https://generativelanguage.googleapis.com/v1beta/openai", model: "gemini-2.5-flash", api_format: "openai", back_model: "gemini-2.5-pro" },
  openrouter: { api_base: "https://openrouter.ai/api/v1", model: "anthropic/claude-sonnet-4.6", api_format: "openai", back_model: "anthropic/claude-opus-4.6" },
  siliconflow: { api_base: "https://api.siliconflow.cn/v1", model: "deepseek-ai/DeepSeek-V3", api_format: "openai", back_model: "deepseek-ai/DeepSeek-R1" },
  ollama: { api_base: "http://127.0.0.1:11434/v1", model: "qwen2.5:14b", api_format: "openai", back_model: "qwen2.5:14b" },
};

function getApiPresetsMap() {
  const arr = (currentState && currentState.api_presets) || (latestSelfCheck && latestSelfCheck.api_presets);
  if (!Array.isArray(arr) || !arr.length) return CLIENT_API_PRESETS;
  const out = { ...CLIENT_API_PRESETS };
  arr.forEach((p) => {
    if (p && p.id) out[p.id] = p;
  });
  return out;
}

const BUILTIN_PREVIEW_FRAMES = {
  "default-template": {
    dialogueStyle: "minimal-glass",
    quoteStyle: "highlight",
    corner: "❖",
    pixelFrame: { enabled: true, scene: "star_sector", weather: "starlight", palette: { "1": "#38bdf8", "2": "#f59e0b", "3": "#1e293b" }, sprites: [] },
  },
  cyberpunk: {
    dialogueStyle: "cyber-chamfer",
    quoteStyle: "bracket-glow",
    corner: "◢",
    pixelFrame: { enabled: true, scene: "cyber_alley", weather: "neon_rain", palette: { "1": "#00f0ff", "2": "#ff2a85", "3": "#ffb800" }, sprites: [] },
  },
  "paper-ink": {
    dialogueStyle: "parchment-scroll",
    quoteStyle: "underline-ink",
    corner: "❖",
    pixelFrame: { enabled: true, scene: "misty_mountains", weather: "fireflies", palette: { "1": "#8c2d19", "2": "#2b5b6c", "3": "#d6cbb9" }, sprites: [] },
  },
  "minimal-dark": {
    dialogueStyle: "terminal-crt",
    quoteStyle: "highlight",
    corner: "⌜",
    pixelFrame: { enabled: true, scene: "rust_wasteland", weather: "embers", palette: { "1": "#88c0d0", "2": "#a3be8c", "3": "#ebcb8b" }, sprites: [] },
  },
};

const DEFAULT_UNIVERSAL_PROMPT =
  "【写作提醒】① 分段自然，用常见词，只写和情节有关的，一轮最多一个新名字； ② 因果写明白，主角有反应，人物首次出场交代姓名、身份、关系、外貌； ③ 台词像真人说话，不写金句和排好队的漂亮话； ④ 一轮演完整再停，只停在真正要你拿主意的地方；随口的小事按性格直接写完，不停在揭晓之前或半截反应上；不写升华总结，不列选项； ⑤ 公正裁决，鲁莽或越级就失败并付出代价。";

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function fetchState() {
  const res = await fetch("/api/state");
  currentState = await res.json();
  renderAll();
  ensurePixelStageObserver();
  if (!jobPollTimer) pollJobState();

  // 初次启动自动检测：若未就绪驱动、无标准世界包或尚无存档，自动打开四阶段自检与引导工作台
  const ob = currentState && currentState.onboarding;
  if (ob && ob.needs_onboarding && !hasPromptedOnboardingOnce) {
    hasPromptedOnboardingOnce = true;
    openGenesisModal(ob.recommended_step || 1);
  }
}

let lastRenderedSave = null;
// 页面上只放最近一页回合，更早的按需往前翻；原文一直完整保存在存档里
let olderTurns = [];
// 重新渲染故事时滚动条怎么放："latest" 定位到最新一轮的开头，"keep" 保持原位，对象表示往前翻页后保持视线
let storyScrollIntent = "latest";

function resetPerSaveView() {
  const key = `${currentState.save_name || ""}|${currentState.pack_id || ""}`;
  if (key === lastRenderedSave) return;
  const switched = lastRenderedSave !== null;
  lastRenderedSave = key;
  olderTurns = [];
  storyScrollIntent = "latest";
  if (!switched) return;
  commandReplies = [];
  activePanel = null;
  panelMountToken += 1;
  if (currentHudTab.startsWith("panel:")) currentHudTab = "scene";
}

function renderAll() {
  if (!currentState) return;
  resetPerSaveView();
  applyThemeSelection();
  renderSystemTopBar();
  renderWorldBanner();
  renderWorldHudTabs();
  renderStoryStream();
  renderHudPanel();
  renderTunerDock();
  renderPendingWorlds();
  renderEffortPicker();
  scheduleDialoguePixelFrame();
}

// ---------- 台前思考强度：玩的时候随时切换（命令行和 API 驱动都生效） ----------
const EFFORT_LABELS = { low: "快速", medium: "平衡", high: "深入", auto: "默认" };

function frontEffortOf(fd) {
  return (fd && (fd.effort || fd.cli_effort)) || "low";
}

function renderEffortPicker() {
  const picker = document.getElementById("effort-picker");
  const sel = document.getElementById("effort-selector");
  if (!picker || !sel) return;
  const fd = ((currentState && currentState.config) || {}).front_driver || {};
  // Harness 由外部代理自己决定；别的命令行工具没有思考强度参数
  const usable = (fd.type || "api") === "api" || (fd.type === "cli" && (fd.cli_tool || "claude") === "claude");
  picker.style.display = usable ? "inline-flex" : "none";
  if (usable && document.activeElement !== sel) sel.value = frontEffortOf(fd);
}

async function changeFrontEffort(value) {
  const sel = document.getElementById("effort-selector");
  const fd = ((currentState && currentState.config) || {}).front_driver || {};
  const previous = frontEffortOf(fd);
  sel.disabled = true;
  try {
    // 只发这一个字段，服务器会合并进现有的台前配置，不动其他设置
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ front_driver: { effort: value } }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "没改成");
    currentState = data.hud;
    renderAll();
    showToast(`思考强度改为「${EFFORT_LABELS[value] || value}」，从下一轮起生效`);
  } catch (e) {
    sel.value = previous;
    showError(`思考强度没改成：${describeFetchError(e)}`);
  } finally {
    sel.disabled = false;
  }
}

// ==================== 主题、字体、对话框样式、四周留白像素绘画与动态情境切换引擎 ====================

// 玩家手动选的氛围只是这台浏览器的显示偏好：不写存档、不进回合快照，选回「自动」后继续跟随剧情
function moodLockKey() {
  return `rpg.moodLock.${(currentState && currentState.save_name) || ""}`;
}

function readMoodLock() {
  const key = moodLockKey();
  try {
    return window.localStorage.getItem(key) || "";
  } catch (_) {
    return moodLockMemory[key] || "";
  }
}

function writeMoodLock(value) {
  const key = moodLockKey();
  moodLockMemory[key] = value;
  try {
    if (value) window.localStorage.setItem(key, value);
    else window.localStorage.removeItem(key);
  } catch (_) {}
}

function getStoryMoodKey() {
  return (currentState && currentState.now && currentState.now.ui_mood) || "default";
}

function getActiveMoodKey() {
  const themeObj = (currentState && currentState.theme) || {};
  const presets = (themeObj.mood_presets && typeof themeObj.mood_presets === "object") ? themeObj.mood_presets : {};
  const locked = readMoodLock();
  if (locked && presets[locked]) return locked;
  const fromNow = getStoryMoodKey();
  if (presets[fromNow]) return fromNow;
  if (presets.default) return "default";
  const keys = Object.keys(presets);
  return keys.length ? keys[0] : "default";
}

function getActiveMoodPreset() {
  const themeObj = (currentState && currentState.theme) || {};
  const presets = (themeObj.mood_presets && typeof themeObj.mood_presets === "object") ? themeObj.mood_presets : {};
  const key = getActiveMoodKey();
  return (presets[key] && typeof presets[key] === "object") ? presets[key] : null;
}

function getActivePixelFrameConfig() {
  const selEl = document.getElementById("theme-selector");
  const sel = selEl ? selEl.value : "pack";
  if (sel === "pack") {
    const themeObj = (currentState && currentState.theme) || {};
    const basePf =
      themeObj.pixel_frame && typeof themeObj.pixel_frame === "object"
        ? themeObj.pixel_frame
        : BUILTIN_PREVIEW_FRAMES["default-template"].pixelFrame;
    const mood = getActiveMoodPreset();
    if (!mood) return basePf;
    const mergedPf = Object.assign({}, basePf);
    if (mood.weather) mergedPf.weather = mood.weather;
    if (mood.palette && typeof mood.palette === "object") {
      mergedPf.palette = Object.assign({}, basePf.palette || {}, mood.palette);
    } else if (mood.css_vars && typeof mood.css_vars === "object") {
      const pal = Object.assign({}, basePf.palette || {});
      if (mood.css_vars["--accent-primary"]) pal["1"] = mood.css_vars["--accent-primary"];
      if (mood.css_vars["--accent-secondary"]) pal["2"] = mood.css_vars["--accent-secondary"];
      mergedPf.palette = pal;
    }
    return mergedPf;
  }
  const preset = BUILTIN_PREVIEW_FRAMES[sel] || BUILTIN_PREVIEW_FRAMES["default-template"];
  return preset.pixelFrame;
}

function applyThemeSelection() {
  const selEl = document.getElementById("theme-selector");
  const sel = selEl ? selEl.value : "pack";
  const root = document.documentElement;
  root.style.cssText = "";

  if (sel === "pack") {
    root.setAttribute("data-theme", "default-template");
    const themeObj = (currentState && currentState.theme) || {};
    const vars = Object.assign({}, themeObj.css_vars || {});
    const mood = getActiveMoodPreset();
    if (mood && mood.css_vars && typeof mood.css_vars === "object") {
      Object.assign(vars, mood.css_vars);
    }
    Object.entries(vars).forEach(([k, v]) => {
      if (k.startsWith("--") && v !== null && v !== undefined) {
        root.style.setProperty(k, String(v));
      }
    });

    const typo = themeObj.typography || {};
    if (typo.font_body || themeObj.font_body) {
      root.style.setProperty("--font-body", typo.font_body || themeObj.font_body);
    }
    if (typo.font_heading) root.style.setProperty("--font-heading", typo.font_heading);
    if (typo.font_dialogue) root.style.setProperty("--font-dialogue", typo.font_dialogue);
    if (themeObj.font_mono && !vars["--font-mono"]) {
      root.style.setProperty("--font-mono", themeObj.font_mono);
    }
    if (typo.story_font_size) root.style.setProperty("--story-font-size", String(typo.story_font_size));
    if (typo.story_line_height) root.style.setProperty("--story-line-height", String(typo.story_line_height));
    if (typo.letter_spacing) root.style.setProperty("--story-letter-spacing", String(typo.letter_spacing));

    const dbox = themeObj.dialogue_box || {};
    root.setAttribute("data-dialogue-style", dbox.style || "cyber-chamfer");
    root.setAttribute("data-quote-style", dbox.quote_style || "highlight");
    if (dbox.max_width) root.style.setProperty("--dialogue-max-width", String(dbox.max_width));
    if (dbox.gutter_x) root.style.setProperty("--dialogue-gutter-x", String(dbox.gutter_x));
    if (dbox.border_width) root.style.setProperty("--dialogue-border-width", String(dbox.border_width));
    const glow = (mood && mood.border_glow) || dbox.glow_color;
    if (glow) root.style.setProperty("--dialogue-glow", String(glow));
    currentCornerOrnament = dbox.corner_ornament !== undefined ? String(dbox.corner_ornament) : "◢";
  } else {
    root.setAttribute("data-theme", sel);
    const preset = BUILTIN_PREVIEW_FRAMES[sel] || BUILTIN_PREVIEW_FRAMES["default-template"];
    root.setAttribute("data-dialogue-style", preset.dialogueStyle);
    root.setAttribute("data-quote-style", preset.quoteStyle);
    currentCornerOrnament = preset.corner || "";
  }

  applyOnAccentColor();
  syncCardCornerOrnaments();
  scheduleDialoguePixelFrame();
}

function applyOnAccentColor() {
  const root = document.documentElement;
  const accent = getComputedStyle(root).getPropertyValue("--accent-primary").trim();
  const probe = document.createElement("canvas").getContext("2d");
  probe.fillStyle = accent;
  probe.fillRect(0, 0, 1, 1);
  const [r, g, b] = probe.getImageData(0, 0, 1, 1).data;
  const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  root.style.setProperty("--on-accent", luminance > 0.55 ? "#0b0e14" : "#ffffff");
}

function onChangeWorldMood(moodValue) {
  const themeSel = document.getElementById("theme-selector");
  if (themeSel && themeSel.value !== "pack") {
    themeSel.value = "pack";
  }
  writeMoodLock(!moodValue || moodValue === "__auto__" ? "" : moodValue);
  applyThemeSelection();
  renderWorldBanner();
}

function syncCardCornerOrnaments() {
  document.querySelectorAll(".turn-card").forEach((card) => {
    if (currentCornerOrnament) {
      card.setAttribute("data-corner", currentCornerOrnament);
    } else {
      card.removeAttribute("data-corner");
    }
  });
}

function switchTheme() {
  applyThemeSelection();
}

function ensurePixelStageObserver() {
  if (stageResizeObserver) return;
  const vp = document.getElementById("dialogue-stage-viewport");
  if (!vp || typeof ResizeObserver === "undefined") return;
  stageResizeObserver = new ResizeObserver(() => {
    renderDialoguePixelFrame();
  });
  stageResizeObserver.observe(vp);
}

function scheduleDialoguePixelFrame() {
  renderDialoguePixelFrame();
  const pf = getActivePixelFrameConfig();
  const weather = (pf && pf.enabled !== false && pf.weather) || "none";
  if (pixelAnimTimer) {
    clearInterval(pixelAnimTimer);
    pixelAnimTimer = null;
  }
  if (weather && weather !== "none") {
    pixelAnimTimer = setInterval(() => {
      if (document.hidden) return;
      pixelAnimTick = (pixelAnimTick + 1) % 10000;
      renderDialoguePixelFrame();
    }, 150);
  }
}

function _cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function _drawPixelSpriteMatrix(ctx, sprite, palette, gx, gy, unitScale) {
  const rows = Array.isArray(sprite.rows) ? sprite.rows : [];
  if (!rows.length) return;
  const sc = Math.max(2, Math.min(8, Number(sprite.scale || unitScale || 4)));
  for (let r = 0; r < rows.length; r++) {
    const line = String(rows[r] || "");
    for (let c = 0; c < line.length; c++) {
      const ch = line[c];
      if (!ch || ch === "." || ch === " " || ch === "_") continue;
      const color = (palette && palette[ch]) || (ch === "1" ? _cssVar("--accent-primary", "#38bdf8") : _cssVar("--accent-secondary", "#f59e0b"));
      ctx.fillStyle = color;
      ctx.fillRect(Math.round(gx + c * sc), Math.round(gy + r * sc), sc, sc);
    }
  }
}

function renderDialoguePixelFrame() {
  const canvas = document.getElementById("dialogue-pixel-canvas");
  const vp = document.getElementById("dialogue-stage-viewport");
  if (!canvas || !vp) return;

  const W = vp.clientWidth;
  const H = vp.clientHeight;
  if (W < 40 || H < 40) return;
  if (canvas.width !== W || canvas.height !== H) {
    canvas.width = W;
    canvas.height = H;
  }

  const ctx = canvas.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  ctx.clearRect(0, 0, W, H);

  const pf = getActivePixelFrameConfig();
  if (!pf || pf.enabled === false) return;

  // 计算中间对话框正文的左右边界，确保像素绘画集中在对话框四周留白区域
  const firstCard = document.querySelector("#story-stream .turn-card");
  let boxLeft = Math.max(56, Math.floor((W - 740) / 2));
  let boxRight = W - boxLeft;
  if (firstCard && firstCard.offsetWidth > 120) {
    const vpRect = vp.getBoundingClientRect();
    const cardRect = firstCard.getBoundingClientRect();
    boxLeft = Math.max(24, Math.floor(cardRect.left - vpRect.left));
    boxRight = Math.min(W - 24, Math.ceil(cardRect.right - vpRect.left));
  }

  const P = 4; // 4x4 整数像素点阵
  const accent1 = (pf.palette && pf.palette["1"]) || _cssVar("--accent-primary", "#38bdf8");
  const accent2 = (pf.palette && pf.palette["2"]) || _cssVar("--accent-secondary", "#f59e0b");
  const accent3 = (pf.palette && pf.palette["3"]) || _cssVar("--accent-warn", "#ffb800");
  const silDark = (pf.palette && pf.palette["4"]) || _cssVar("--bg-panel", "#151c28");
  const silMid = (pf.palette && pf.palette["5"]) || _cssVar("--border-color", "#263349");
  const scene = pf.scene || "cyber_alley";

  const drawDot = (x, y, wDots, hDots, color, alpha = 1) => {
    ctx.globalAlpha = alpha;
    ctx.fillStyle = color;
    ctx.fillRect(Math.floor(x / P) * P, Math.floor(y / P) * P, wDots * P, hDots * P);
    ctx.globalAlpha = 1;
  };

  // 1. 绘制对话框左右留白区的像素场景底画
  const leftW = Math.max(0, boxLeft - 10);
  const rightX = Math.min(W, boxRight + 10);
  const rightW = Math.max(0, W - rightX);

  if (leftW >= 24 || rightW >= 24) {
    if (scene === "cyber_alley") {
      const towersL = [0.68, 0.46, 0.82, 0.55];
      const towersR = [0.58, 0.78, 0.44, 0.72];
      towersL.forEach((ratio, i) => {
        const bw = Math.max(P * 3, Math.floor(leftW / 4));
        const bx = i * Math.floor(leftW / 4);
        const bh = Math.floor(H * ratio);
        if (bx + bw <= leftW + P) {
          ctx.fillStyle = i % 2 === 0 ? silDark : silMid;
          ctx.fillRect(bx, H - bh, bw, bh);
          for (let wy = H - bh + P * 3; wy < H - P * 4; wy += P * 4) {
            if ((wy + i * 7) % 3 === 0) {
              drawDot(bx + P, wy, 1, 1, (wy + i) % 2 === 0 ? accent1 : accent2, 0.55);
            }
          }
        }
      });
      towersR.forEach((ratio, i) => {
        const bw = Math.max(P * 3, Math.floor(rightW / 4));
        const bx = rightX + i * Math.floor(rightW / 4);
        const bh = Math.floor(H * ratio);
        if (bx < W) {
          ctx.fillStyle = i % 2 === 1 ? silDark : silMid;
          ctx.fillRect(bx, H - bh, bw, bh);
          for (let wy = H - bh + P * 3; wy < H - P * 4; wy += P * 4) {
            if ((wy + i * 5) % 3 === 0) {
              drawDot(bx + P, wy, 1, 1, (wy + i) % 2 === 0 ? accent1 : accent3, 0.55);
            }
          }
        }
      });
      // 顶部像素电缆横线
      for (let x = 0; x < leftW; x += P) {
        const sag = Math.sin((x / Math.max(1, leftW)) * Math.PI) * P * 3;
        drawDot(x, 28 + sag, 1, 1, silMid, 0.85);
      }
      for (let x = rightX; x < W; x += P) {
        const sag = Math.sin(((x - rightX) / Math.max(1, rightW)) * Math.PI) * P * 3;
        drawDot(x, 34 + sag, 1, 1, silMid, 0.85);
      }
    } else if (scene === "misty_mountains") {
      // 远山层叠与云带像素画
      for (let x = 0; x < leftW; x += P) {
        const h1 = Math.floor(H * 0.34 + Math.sin(x * 0.045) * 34);
        const h2 = Math.floor(H * 0.22 + Math.cos(x * 0.07) * 22);
        drawDot(x, H - h1, 1, Math.ceil(h1 / P), silMid, 0.35);
        drawDot(x, H - h2, 1, Math.ceil(h2 / P), silDark, 0.65);
      }
      for (let x = rightX; x < W; x += P) {
        const h1 = Math.floor(H * 0.36 + Math.cos(x * 0.045) * 34);
        const h2 = Math.floor(H * 0.24 + Math.sin(x * 0.065) * 22);
        drawDot(x, H - h1, 1, Math.ceil(h1 / P), silMid, 0.35);
        drawDot(x, H - h2, 1, Math.ceil(h2 / P), silDark, 0.65);
      }
    } else if (scene === "gothic_cathedral") {
      // 哥特尖拱立柱与花窗点阵
      [12, Math.max(28, leftW - 28)].forEach((lx) => {
        if (lx + P * 3 < leftW) {
          drawDot(lx, 24, 3, Math.floor((H - 24) / P), silDark, 0.78);
          drawDot(lx + P, 48, 1, 12, accent2, 0.5);
        }
      });
      [rightX + 12, Math.max(rightX + 28, W - 28)].forEach((rx) => {
        if (rx + P * 3 < W) {
          drawDot(rx, 24, 3, Math.floor((H - 24) / P), silDark, 0.78);
          drawDot(rx + P, 48, 1, 12, accent1, 0.5);
        }
      });
    } else if (scene === "rust_wasteland") {
      // 废土残骸斜梁与信号塔
      for (let x = 0; x < leftW; x += P * 2) {
        const dune = Math.floor(H * 0.18 + Math.sin(x * 0.05) * 14);
        drawDot(x, H - dune, 2, Math.ceil(dune / P), silDark, 0.75);
      }
      for (let x = rightX; x < W; x += P * 2) {
        const dune = Math.floor(H * 0.2 + Math.cos(x * 0.05) * 14);
        drawDot(x, H - dune, 2, Math.ceil(dune / P), silDark, 0.75);
      }
      if (leftW > 36) {
        drawDot(Math.floor(leftW * 0.45), H - 150, 1, 30, silMid, 0.85);
        drawDot(Math.floor(leftW * 0.45), H - 154, 1, 1, pixelAnimTick % 2 === 0 ? accent3 : accent2, 0.95);
      }
    } else if (scene === "star_sector") {
      // 深空轨道桁架与静态星点
      for (let i = 0; i < 28; i++) {
        const sx = ((i * 73) % Math.max(16, leftW));
        const sy = ((i * 137) % H);
        drawDot(sx, sy, 1, 1, i % 3 === 0 ? accent1 : "#cbd5e1", 0.45);
        const rx = rightX + ((i * 89) % Math.max(16, rightW));
        const ry = ((i * 151) % H);
        if (rx < W) drawDot(rx, ry, 1, 1, i % 4 === 0 ? accent2 : "#cbd5e1", 0.45);
      }
    }

    // 对话框外缘像素装饰立线（勾勒留白边界）
    for (let y = 18; y < H - 18; y += P * 3) {
      drawDot(boxLeft - P * 2, y, 1, 1, silMid, 0.45);
      drawDot(boxRight + P, y, 1, 1, silMid, 0.45);
    }
  }

  // 2. 绘制像素天气微粒（仅在左右留白区与顶底边缘轻盈律动，绝不干扰正文阅读）
  const weather = pf.weather || "none";
  if (weather !== "none" && (leftW >= 16 || rightW >= 16)) {
    for (let i = 0; i < 18; i++) {
      const inLeft = i % 2 === 0;
      const spanW = inLeft ? leftW : rightW;
      const baseX = inLeft ? 4 : rightX + 4;
      if (spanW < 12) continue;
      const px = baseX + ((i * 47 + (weather === "snow" ? pixelAnimTick * 2 : 0)) % Math.max(8, spanW - 8));
      if (weather === "neon_rain") {
        const py = (i * 61 + pixelAnimTick * 18) % H;
        drawDot(px, py, 1, 3, i % 3 === 0 ? accent2 : accent1, 0.52);
      } else if (weather === "embers") {
        const py = H - ((i * 53 + pixelAnimTick * 8) % H);
        drawDot(px, py, 1, 1, i % 2 === 0 ? accent3 : accent2, 0.72);
      } else if (weather === "snow") {
        const py = (i * 67 + pixelAnimTick * 6) % H;
        drawDot(px, py, 1, 1, "#e2e8f0", 0.65);
      } else if (weather === "fireflies" || weather === "starlight") {
        const py = (i * 83) % (H - 24) + 12;
        const blink = ((pixelAnimTick + i) % 5) < 3 ? 0.78 : 0.18;
        drawDot(px, py, 1, 1, i % 2 === 0 ? accent1 : accent3, blink);
      } else if (weather === "fog") {
        const py = (i * 59) % H;
        drawDot(px, py, 4, 1, silMid, 0.28);
      }
    }
  }

  // 3. 渲染 AI 在 theme.json 中原创绘制的字符矩阵像素精灵 (pixel_frame.sprites)
  const sprites = Array.isArray(pf.sprites) ? pf.sprites : [];
  const palette = pf.palette || {};
  sprites.forEach((sp) => {
    if (!sp || !Array.isArray(sp.rows) || !sp.rows.length) return;
    const sc = Math.max(2, Math.min(8, Number(sp.scale || 4)));
    const sw = String(sp.rows[0] || "").length * sc;
    const sh = sp.rows.length * sc;
    const anchor = sp.anchor || "left-top";
    let sx = 12;
    let sy = 22;

    if (anchor === "left-top") {
      sx = Math.max(8, Math.floor((leftW - sw) / 2));
      sy = 24;
    } else if (anchor === "left-mid") {
      sx = Math.max(8, Math.floor((leftW - sw) / 2));
      sy = Math.floor((H - sh) / 2);
    } else if (anchor === "left-bottom") {
      sx = Math.max(8, Math.floor((leftW - sw) / 2));
      sy = Math.max(24, H - sh - 22);
    } else if (anchor === "right-top") {
      sx = rightX + Math.max(8, Math.floor((rightW - sw) / 2));
      sy = 24;
    } else if (anchor === "right-mid") {
      sx = rightX + Math.max(8, Math.floor((rightW - sw) / 2));
      sy = Math.floor((H - sh) / 2);
    } else if (anchor === "right-bottom") {
      sx = rightX + Math.max(8, Math.floor((rightW - sw) / 2));
      sy = Math.max(24, H - sh - 22);
    } else if (anchor === "box-corner-tl") {
      sx = Math.max(4, boxLeft - sw - 8);
      sy = 16;
    } else if (anchor === "box-corner-tr") {
      sx = Math.min(W - sw - 4, boxRight + 8);
      sy = 16;
    } else if (anchor === "box-corner-bl") {
      sx = Math.max(4, boxLeft - sw - 8);
      sy = Math.max(16, H - sh - 18);
    } else if (anchor === "box-corner-br") {
      sx = Math.min(W - sw - 4, boxRight + 8);
      sy = Math.max(16, H - sh - 18);
    }

    if ((anchor.startsWith("left") && leftW < sw + 4) || (anchor.startsWith("right") && rightW < sw + 4)) {
      return;
    }
    _drawPixelSpriteMatrix(ctx, sp, palette, sx, sy, sc);
  });
}

// ==================== 固定系统配置栏 ====================

function renderSystemTopBar() {
  const saveSel = document.getElementById("save-selector");
  if (saveSel) {
    const saves = currentState.saves || [];
    if (!saves.length) {
      saveSel.innerHTML = `<option value="">还没有存档</option>`;
    } else {
      saveSel.innerHTML = "";
      saves.forEach((s) => {
        const opt = document.createElement("option");
        opt.value = s.name;
        // 这一局和世界同名时不重复显示，不同名时才标出是哪个世界
        const world = (currentState.packs || []).find((p) => p.id === s.pack);
        const worldName = world ? world.name : s.pack;
        opt.textContent = worldName && worldName !== s.name ? `${s.name} · ${worldName}` : s.name;
        if (s.is_current) opt.selected = true;
        saveSel.appendChild(opt);
      });
    }
    saveSel.onchange = async (e) => {
      if (!e.target.value) return;
      const res = await fetch("/api/saves/select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: e.target.value }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        showError(data.detail || "切换存档失败");
      }
      await fetchState();
    };
  }
}

function toggleTunerDock() {
  const ws = document.getElementById("main-workspace");
  const btn = document.getElementById("tuner-dock-toggle");
  if (!ws) return;
  ws.classList.toggle("tuner-collapsed");
  const open = !ws.classList.contains("tuner-collapsed");
  if (btn) btn.classList.toggle("active-dock-btn", open);
  try {
    window.localStorage.setItem("rpg.tunerDockOpen", open ? "1" : "0");
  } catch (_) {}
}

// ==================== 定制化世界横幅与 6 类自适应状态控件 ====================

function renderPackQuickCommands() {
  const box = document.getElementById("pack-quick-commands");
  if (!box) return;
  const items = (currentState && currentState.ui_schema && currentState.ui_schema.quick_commands) || [];
  box.innerHTML = items
    .filter((it) => it && it.command)
    .map((it) => {
      const cmd = String(it.command);
      const fill = escapeHtml(JSON.stringify(cmd.endsWith(" ") ? cmd : `${cmd} `));
      return `<button onclick="fillCommand(${fill})" title="${escapeHtml(it.hint || cmd)}">${escapeHtml(it.label || cmd)}</button>`;
    })
    .join("");
}

function renderWorldBanner() {
  renderPackQuickCommands();
  const brandEl = document.getElementById("world-brand-title");
  if (brandEl) {
    brandEl.textContent = currentState.world_title || "AI-RPG-PLAYER";
    document.title = `${currentState.world_title || "AI-RPG-Player"} · 通用文字跑团引擎`;
  }

  const taglineEl = document.getElementById("world-tagline");
  if (taglineEl) {
    const tl = (currentState.ui_schema && currentState.ui_schema.tagline) || "";
    taglineEl.textContent = tl;
    taglineEl.style.display = tl ? "block" : "none";
  }

  const warnBadge = document.getElementById("pack-missing-badge");
  if (warnBadge) {
    if (currentState.pack_missing) {
      warnBadge.style.display = "inline-block";
      warnBadge.textContent = `⚠ 原世界包「${currentState.missing_pack_id}」已卸载，已平滑切换存档内置设定兜底`;
    } else {
      warnBadge.style.display = "none";
    }
  }

  const st = (currentState.statusline && currentState.statusline.state) || null;
  const px = (currentState.statusline && currentState.statusline.pixels) || [];
  drawSkyline(px);

  const pillsBox = document.getElementById("status-bar-pills");
  const themeObj = (currentState && currentState.theme) || {};
  const moodPresets = (themeObj.mood_presets && typeof themeObj.mood_presets === "object") ? themeObj.mood_presets : {};
  const moodKeys = Object.keys(moodPresets);
  let moodSelectHtml = "";
  if (moodKeys.length > 0) {
    const locked = readMoodLock();
    const isLocked = !!(locked && moodPresets[locked]);
    const storyMood = moodPresets[getStoryMoodKey()] || {};
    const opts = moodKeys
      .map((mk) => {
        const mp = moodPresets[mk] || {};
        const selected = isLocked && mk === locked ? "selected" : "";
        return `<option value="${escapeHtml(mk)}" ${selected}>${escapeHtml(mp.label || mk)}</option>`;
      })
      .join("");
    const autoLabel = `自动 · ${storyMood.label || getStoryMoodKey()}`;
    moodSelectHtml = `<select class="mood-pill-select" title="氛围主题：默认由主持人随剧情切换；手动选择后固定显示，直到选回自动" onchange="onChangeWorldMood(this.value)">
      <option value="__auto__" ${isLocked ? "" : "selected"}>${escapeHtml(autoLabel)}</option>${opts}
    </select>`;
  }

  if (!st) {
    const needOb = currentState.onboarding && currentState.onboarding.needs_onboarding;
    pillsBox.innerHTML = needOb
      ? `${moodSelectHtml}<span class="pill warn">尚未安装世界包</span><button class="primary" onclick="openGenesisModal(1)">✨ 启动创世向导</button>`
      : `${moodSelectHtml}<span class="pill accent">尚未加载存档</span><button onclick="openNewGame()">+ 新建或选择世界存档</button>`;
    return;
  }

  const worldItems = st.world_status_items || [];
  const htmlParts = [];
  if (moodSelectHtml) htmlParts.push(moodSelectHtml);
  worldItems.forEach((item, idx) => htmlParts.push(renderTopWorldWidget(item, idx)));
  if (st.player_name || st.rank) {
    const who = [st.player_name, st.rank].filter(Boolean).join(" · ");
    htmlParts.push(`<span class="stat-block"><span class="stat-label">角色</span><span class="stat-value">${escapeHtml(who)}</span></span>`);
  }
  pillsBox.innerHTML = htmlParts.join("");
}

function renderTopWorldWidget(item, idx) {
  const icon = item.icon ? `${escapeHtml(item.icon)} ` : "";
  const title = `${escapeHtml(item.label)}: ${escapeHtml(item.value)}`;
  if (item.widget === "bar" || item.widget === "meter" || item.widget === "fraction") {
    const fillCls = item.level === "danger" ? "danger" : item.level === "warn" ? "warn" : "";
    return `
      <span class="stat-block" title="${title}">
        <span class="stat-label">${icon}${escapeHtml(item.label)}</span>
        <span class="stat-meter-row">
          <span class="mini-bar-track"><span class="mini-bar-fill ${fillCls}" style="width:${item.percent || 0}%"></span></span>
          <span class="stat-value">${escapeHtml(item.value)}</span>
        </span>
      </span>`;
  }
  const tone = item.tone === "warn" ? "tone-warn" : item.tone === "accent" || idx === 0 ? "tone-accent" : "";
  return `
    <span class="stat-block ${tone}" title="${title}">
      <span class="stat-label">${icon}${escapeHtml(item.label)}</span>
      <span class="stat-value">${escapeHtml(item.value)}</span>
    </span>`;
}

function renderAdaptiveWidget(item) {
  const icon = item.icon ? `${escapeHtml(item.icon)} ` : "";
  const w = item.widget || "kv";

  if (w === "bar" || w === "meter") {
    const fillCls =
      w === "meter"
        ? item.level === "danger"
          ? "danger"
          : item.level === "warn"
          ? "warn"
          : ""
        : item.color || "";
    return `
      <div class="widget-bar-card">
        <div class="widget-head">
          <span class="k">${icon}${escapeHtml(item.label)}</span>
          <span class="v">${escapeHtml(item.value)}</span>
        </div>
        <div class="bar-track">
          <div class="bar-fill ${escapeHtml(fillCls)}" style="width:${item.percent || 0}%"></div>
        </div>
      </div>`;
  }

  if (w === "fraction") {
    return `
      <div class="widget-fraction-card">
        <div class="widget-head">
          <span class="k">${icon}${escapeHtml(item.label)}</span>
          <span class="fraction-nums">${escapeHtml(item.current)} <small>/ ${escapeHtml(item.max)}${escapeHtml(item.unit || "")}</small></span>
        </div>
        <div class="bar-track">
          <div class="bar-fill" style="width:${item.percent || 0}%"></div>
        </div>
      </div>`;
  }

  if (w === "tags") {
    const tags = item.tags || [];
    const tagsHtml = tags.length
      ? tags.map((t) => `<span class="state-tag">${escapeHtml(t)}</span>`).join("")
      : `<span style="color:var(--text-muted);font-size:0.78rem;">无特殊状态</span>`;
    return `
      <div class="widget-tags-card">
        <div class="widget-head"><span class="k">${icon}${escapeHtml(item.label)}</span></div>
        <div class="tags-wrap">${tagsHtml}</div>
      </div>`;
  }

  return `
    <div class="status-kv-row">
      <span class="k">${icon}${escapeHtml(item.label)}</span>
      <span class="v">${escapeHtml(item.value || "—")}</span>
    </div>`;
}

// ==================== 定制化世界面板 Tab 与模块化角色卡 ====================

function renderWorldHudTabs() {
  const tabsCfg = (currentState.ui_schema && currentState.ui_schema.hud_tabs) || {};
  const defaults = {
    scene: { label: "日志卡", icon: "📓" },
    sheet: { label: "角色状态", icon: "👤" },
    persona: { label: "性格", icon: "🎭" },
    milestones: { label: "重要时刻", icon: "🌟" },
  };
  document.querySelectorAll("#world-hud-tabs .hud-tab[data-panel]").forEach((b) => b.remove());
  document.querySelectorAll("#world-hud-tabs .hud-tab").forEach((btn) => {
    const key = btn.getAttribute("data-tab");
    const cfg = Object.assign({}, defaults[key] || {}, tabsCfg[key] || {});
    btn.textContent = cfg.label || key;
    btn.classList.toggle("active", key === currentHudTab);
  });
  const panels = (currentState.has_save && currentState.ui_schema && currentState.ui_schema.panels) || [];
  const tabsBox = document.getElementById("world-hud-tabs");
  panels.forEach((p) => {
    const key = `panel:${p.id}`;
    const btn = document.createElement("button");
    btn.className = `hud-tab${key === currentHudTab ? " active" : ""}`;
    btn.dataset.panel = p.id;
    btn.dataset.tab = key;
    btn.textContent = p.label || p.id;
    btn.onclick = () => switchHudTab(key, btn);
    tabsBox.appendChild(btn);
  });
  if (currentHudTab.startsWith("panel:") && !panels.some((p) => `panel:${p.id}` === currentHudTab)) {
    currentHudTab = "scene";
    const sceneBtn = tabsBox.querySelector('[data-tab="scene"]');
    if (sceneBtn) sceneBtn.classList.add("active");
  }
}

// ---------- 自定义面板：世界包或玩家的 AI 写的 HTML，放进浏览器沙箱运行 ----------
// sandbox 只开 allow-scripts：面板是独立的匿名来源，读不到本页面、存档和浏览器存储。
// CSP 禁止一切网络请求。面板只能拿到 panel_state 白名单里的数据，只能向外发 ready/fill_input/error 三种消息。

const PANEL_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; media-src data:; form-action 'none'; base-uri 'none'";
const PANEL_CSS_VARS = ["--bg-main", "--bg-panel", "--bg-card", "--bg-input", "--text-main", "--text-muted", "--accent-primary", "--accent-secondary", "--accent-warn", "--accent-danger", "--accent-ok", "--border-color", "--on-accent", "--font-body", "--font-heading", "--font-mono"];
const PANEL_BRIDGE = `(function () {
  var state = null, subs = [];
  function send(msg) { parent.postMessage(msg, "*"); }
  window.RPG = {
    get state() { return state; },
    onState: function (cb) { subs.push(cb); if (state) cb(state); },
    fillInput: function (text) { send({ rpg: "fill_input", text: String(text).slice(0, 2000) }); }
  };
  window.addEventListener("message", function (e) {
    if (e.source !== parent || !e.data || e.data.rpg !== "state") return;
    state = e.data.state;
    var vars = e.data.vars || {};
    Object.keys(vars).forEach(function (k) { document.documentElement.style.setProperty(k, vars[k]); });
    subs.forEach(function (cb) { try { cb(state); } catch (err) { send({ rpg: "error", message: String(err) }); } });
  });
  window.addEventListener("error", function (e) { send({ rpg: "error", message: String(e.message) }); });
  window.addEventListener("DOMContentLoaded", function () { send({ rpg: "ready" }); });
})();`;

let activePanel = null;
let panelMountToken = 0;

function panelCssVars() {
  const computed = getComputedStyle(document.documentElement);
  const vars = {};
  PANEL_CSS_VARS.forEach((k) => {
    vars[k] = computed.getPropertyValue(k).trim();
  });
  return vars;
}

function postPanelState() {
  if (!activePanel || !activePanel.ready || !activePanel.frame.contentWindow) return;
  const state = Object.assign({}, (currentState && currentState.panel_state) || {});
  const data = state.data || {};
  state.data = data[activePanel.id] === undefined ? null : data[activePanel.id]; // 每个面板只拿自己的数据
  activePanel.frame.contentWindow.postMessage({ rpg: "state", state, vars: panelCssVars() }, "*");
}

function showPanelProblem(message) {
  const bar = document.getElementById("panel-problem");
  if (bar) {
    bar.textContent = message;
    bar.style.display = "block";
  }
}

function buildPanelDocument(html) {
  // 面板内容放在 CSP 之后，它不能放宽 CSP；用字符串拼接避免 </script> 出现在本文件的脚本里
  const close = "</" + "script>";
  return `<!doctype html><html><head><meta charset="utf-8" />
<meta http-equiv="Content-Security-Policy" content="${PANEL_CSP}" />
<style>html,body{margin:0;background:var(--bg-panel);color:var(--text-main);font:13px/1.6 var(--font-body,sans-serif);}body{padding:14px 16px;}</style>
<script>${PANEL_BRIDGE}${close}
</head><body>${html}</body></html>`;
}

async function mountPanel(panelId, host) {
  const token = ++panelMountToken;
  activePanel = null;
  host.innerHTML = `<div class="panel-host"><div id="panel-problem" class="panel-problem" style="display:none;"></div><div class="panel-loading">正在加载面板…</div></div>`;
  try {
    const res = await fetch("/api/panels/source", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: panelId }),
    });
    const data = await res.json();
    if (token !== panelMountToken) return;
    if (!res.ok) throw new Error(data.detail || "面板加载失败");

    const frame = document.createElement("iframe");
    frame.className = "panel-frame";
    frame.setAttribute("sandbox", "allow-scripts");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.title = panelId;
    const panel = { id: panelId, frame, ready: false, loads: 0 };
    frame.addEventListener("load", () => {
      panel.loads += 1;
      if (panel.loads > 1) {
        // 面板把自己跳转走了：内容已不可信，直接卸载
        frame.remove();
        if (activePanel === panel) activePanel = null;
        showPanelProblem("这个面板试图跳转到别的页面，已被卸载。");
      }
    });
    activePanel = panel;
    frame.srcdoc = buildPanelDocument(data.html); // 先设内容再插入，否则空白页也会触发一次 load
    host.querySelector(".panel-host").replaceChildren(host.querySelector("#panel-problem"), frame);
    setTimeout(() => {
      if (activePanel === panel && !panel.ready) showPanelProblem("这个面板没有正常启动，可能有语法错误。");
    }, 5000);
  } catch (e) {
    if (token === panelMountToken) showPanelProblem(`面板加载失败：${describeFetchError(e)}`);
    const loading = host.querySelector(".panel-loading");
    if (loading) loading.remove();
  }
}

window.addEventListener("message", (ev) => {
  if (!activePanel || ev.source !== activePanel.frame.contentWindow || !ev.data) return;
  if (ev.data.rpg === "ready") {
    activePanel.ready = true;
    postPanelState();
  } else if (ev.data.rpg === "fill_input") {
    // 只填进输入框，玩家确认后才会发送
    const input = document.getElementById("action-input");
    if (input && !activeTurnAbortCtrl) {
      input.value = String(ev.data.text || "").slice(0, 2000);
      input.focus();
    }
  } else if (ev.data.rpg === "error") {
    showPanelProblem(`面板出错：${String(ev.data.message || "").slice(0, 200)}`);
  }
});

function switchHudTab(tab, btn) {
  currentHudTab = tab;
  document.querySelectorAll("#world-hud-tabs .hud-tab").forEach((b) => b.classList.remove("active"));
  if (btn) btn.classList.add("active");
  renderHudPanel();
}

function renderHudPanel() {
  const panel = document.getElementById("hud-panel");
  if (!currentState || !currentState.has_save) {
    panel.innerHTML = `
      <div class="hud-section">
        <h4><span>还没有开始游戏</span></h4>
        <button class="primary" style="width:100%;" onclick="openGenesisModal()">打开开局向导</button>
      </div>`;
    return;
  }

  panel.classList.toggle("panel-mode", currentHudTab.startsWith("panel:"));
  if (currentHudTab.startsWith("panel:")) {
    const panelId = currentHudTab.slice("panel:".length);
    if (activePanel && activePanel.id === panelId && activePanel.frame.isConnected) {
      postPanelState();
    } else {
      mountPanel(panelId, panel);
    }
    return;
  }
  activePanel = null;
  panelMountToken += 1;
  if (currentHudTab === "scene") {
    const now = currentState.now || {};
    const npcs = (now.present_npcs || [])
      .map(
        (n) => `
        <div class="npc-chip">
          <div><strong>${escapeHtml(n.name)}</strong> <span style="color:var(--text-muted);font-size:0.78rem;">(${escapeHtml(n.rank || "未知")})</span></div>
          <div style="font-size:0.8rem;color:var(--text-muted);">${escapeHtml(n.brief || "")}</div>
          ${n.activity ? `<div style="font-size:0.78rem;margin-top:2px;">动向：${escapeHtml(n.activity)}</div>` : ""}
        </div>`
      )
      .join("");

    const journalCards = (currentState.journal_cards || [])
      .filter((c) => c.content)
      .map((c, i) => {
        const count = c.content.split(/\r?\n/).filter((l) => l.trim().startsWith("-")).length;
        const open = journalSectionOpen(c.title, i === 0);
        return `
        <details class="hud-section journal-card" ${open ? "open" : ""} data-title="${escapeHtml(c.title)}" ontoggle="rememberJournalSection(this)">
          <summary ${c.hint ? `title="${escapeHtml(c.hint)}"` : ""}>
            <span>${escapeHtml(c.icon || "📓")} ${escapeHtml(c.title)}</span>
            ${count ? `<span class="journal-count">${count}</span>` : ""}
          </summary>
          <div class="md-view md-rich">${renderMdLite(c.content)}</div>
        </details>`;
      })
      .join("");

    panel.innerHTML = `
      ${npcs ? `<div class="hud-section"><h4><span>在场人物</span></h4>${npcs}</div>` : ""}
      ${journalCards || `<div class="hud-empty">主角知道的事会随着游戏记在这里。</div>`}`;
  } else if (currentHudTab === "sheet") {
    panel.innerHTML = renderCustomizableCharacterSheet(currentState.player || {});
  } else if (currentHudTab === "persona") {
    panel.innerHTML = `
      <div class="hud-section">
        <h4><span>🎭 主角性格与处事习惯</span></h4>
        <div class="md-view md-rich">${renderMdLite(currentState.persona || "（随游玩选择自动总结）")}</div>
      </div>`;
  } else if (currentHudTab === "milestones") {
    panel.innerHTML = `
      <div class="hud-section">
        <h4><span>🌟 一生重要时刻（永不压缩）</span></h4>
        <div class="md-view md-rich">${renderMdLite(currentState.milestones || "")}</div>
      </div>`;
  }
}

// 日志卡每栏展开还是收起，按这台浏览器记住；默认只展开第一栏（当前目标）
function journalSectionOpen(title, fallback) {
  try {
    const v = window.localStorage.getItem(`rpg.journalOpen.${title}`);
    return v === null ? fallback : v === "1";
  } catch (_) {
    return fallback;
  }
}

function rememberJournalSection(el) {
  try {
    window.localStorage.setItem(`rpg.journalOpen.${el.dataset.title}`, el.open ? "1" : "0");
  } catch (_) {}
}

function renderCustomizableCharacterSheet(p) {
  const layout = p.sheet_layout || {};
  const sections = layout.sections || [
    { id: "status_widgets", title: "核心状态指标", visible: true },
    { id: "attributes", title: layout.attributes_title || "基础属性", visible: true },
    { id: "affinities", title: "适性与潜能", visible: true, hide_empty: true },
    { id: "abilities", title: "特殊天赋", visible: true, hide_empty: true },
    { id: "skills", title: "掌握技能", visible: true, hide_empty: true },
    { id: "gear", title: "随身装备", visible: true, hide_empty: false },
    { id: "reputation", title: "势力声望", visible: true, hide_empty: true },
    { id: "inventory", title: "背包物品与备注", visible: true, hide_empty: false },
  ];

  const blocks = [
    `<div class="hud-section">
       <h4><span>👤 ${escapeHtml(p.name || "主角")}</span><span class="hud-hint">${escapeHtml(p.rank || "未定阶")}</span></h4>
       ${(p.status_items || []).length ? `<div class="status-kv-list">${p.status_items.map(renderAdaptiveWidget).join("")}</div>` : ""}
     </div>`,
  ];

  sections.forEach((sec) => {
    if (!sec || sec.visible === false || sec.id === "status_widgets") return;
    const sid = sec.id;
    const hideEmpty = sec.hide_empty !== false;

    if (sid === "attributes") {
      const entries = Object.entries(p.attributes || {});
      if (!entries.length && hideEmpty) return;
      const grid = entries
        .map(([k, v]) => `<div class="stat-item"><span>${escapeHtml(k)}</span><strong>${escapeHtml(v)}</strong></div>`)
        .join("");
      blocks.push(`
        <div class="hud-section">
          <h4><span>📊 ${escapeHtml(sec.title || layout.attributes_title || "基础属性")}</span></h4>
          <div class="stat-grid">${grid || '<span style="color:var(--text-muted)">未初始化</span>'}</div>
        </div>`);
    } else if (sid === "core_engine") {
      const eng = p.core_engine;
      if (!eng && hideEmpty) return;
      const engStr =
        eng && typeof eng === "object"
          ? `${eng.name || ""} (${[eng.element, eng.grade, eng.orientation].filter(Boolean).join(" · ")})`
          : String(eng || "未搭载");
      blocks.push(`
        <div class="hud-section">
          <h4><span>⚙️ ${escapeHtml(sec.title || "核心体系")}</span></h4>
          <div>${escapeHtml(engStr)}</div>
        </div>`);
    } else if (sid === "affinities") {
      const affEntries = Object.entries(p.affinity || {});
      const aptEntries = Object.entries(p.aptitude || {});
      if (!affEntries.length && !aptEntries.length && hideEmpty) return;
      const tags = [
        ...affEntries.map(([k, v]) => `<span class="state-tag">${escapeHtml(k)}: ${escapeHtml(v)}</span>`),
        ...aptEntries.map(([k, v]) => `<span class="state-tag">${escapeHtml(k)}: ${escapeHtml(v)}</span>`),
      ].join("");
      blocks.push(`
        <div class="hud-section">
          <h4><span>🧬 ${escapeHtml(sec.title || "适性与潜能")}</span></h4>
          <div class="tags-wrap">${tags || '<span style="color:var(--text-muted)">无</span>'}</div>
        </div>`);
    } else if (sid === "abilities") {
      const abs = p.abilities || [];
      if (!abs.length && hideEmpty) return;
      const list = abs
        .map((a) => `<li><strong>${escapeHtml(a.name)}</strong> Lv.${escapeHtml(a.level ?? 1)}${a.notes ? " — " + escapeHtml(a.notes) : ""}</li>`)
        .join("");
      blocks.push(`
        <div class="hud-section">
          <h4><span>✨ ${escapeHtml(sec.title || "专属天赋与能力")}</span></h4>
          <ul style="padding-left:16px;">${list || "<li>无</li>"}</ul>
        </div>`);
    } else if (sid === "skills") {
      const sks = p.skills || [];
      if (!sks.length && hideEmpty) return;
      const list = sks
        .map((s) => `<li><strong>${escapeHtml(s.name)}</strong> (${[s.level, s.element, s.type].filter(Boolean).map(escapeHtml).join(" · ")})</li>`)
        .join("");
      blocks.push(`
        <div class="hud-section">
          <h4><span>⚔️ ${escapeHtml(sec.title || "掌握技能")}</span></h4>
          <ul style="padding-left:16px;">${list || "<li>暂无技能</li>"}</ul>
        </div>`);
    } else if (sid === "gear") {
      const gear = p.gear || [];
      if (!gear.length && sec.hide_empty === true) return;
      const list = gear
        .map((g) => `<li><strong>${escapeHtml(g.name)}</strong> ${g.tier ? "[" + escapeHtml(g.tier) + "]" : ""}</li>`)
        .join("");
      blocks.push(`
        <div class="hud-section">
          <h4><span>🛡️ ${escapeHtml(sec.title || "装备与义体")}</span></h4>
          <ul style="padding-left:16px;">${list || '<li style="color:var(--text-muted)">暂无装备</li>'}</ul>
        </div>`);
    } else if (sid === "reputation") {
      const reps = p.reputation_items || [];
      if (!reps.length && hideEmpty) return;
      const rows = reps
        .map((r) => {
          const pct = Math.max(0, Math.min(100, (Number(r.score || 0) + 100) / 2));
          return `
            <div class="widget-bar-card" style="margin-bottom:5px;">
              <div class="widget-head"><span class="k">${escapeHtml(r.name)}</span><span class="v">${r.score > 0 ? "+" + r.score : r.score}</span></div>
              <div class="bar-track"><div class="bar-fill secondary" style="width:${pct}%"></div></div>
            </div>`;
        })
        .join("");
      blocks.push(`
        <div class="hud-section">
          <h4><span>🏛️ ${escapeHtml(sec.title || "各方势力声望")}</span></h4>
          ${rows || '<div style="color:var(--text-muted)">暂无声望记录</div>'}
        </div>`);
    } else if (sid === "inventory") {
      const items = p.items || [];
      if (!items.length && !p.body && sec.hide_empty === true) return;
      const list = items
        .map((it) => `<li>${escapeHtml(it.name || it)}${it.count ? " × " + escapeHtml(it.count) : ""}</li>`)
        .join("");
      blocks.push(`
        <div class="hud-section">
          <h4><span>🎒 ${escapeHtml(sec.title || "随身物品与备注")}</span></h4>
          <ul style="padding-left:16px;margin-bottom:6px;">${list || '<li style="color:var(--text-muted)">空</li>'}</ul>
          ${p.body ? `<div class="md-view md-rich">${renderMdLite(p.body)}</div>` : ""}
        </div>`);
    }
  });

  return blocks.join("");
}

// ==================== 右侧固定独立调教窗口 (On-Demand Tuner Sub-Agent) ====================

function renderModsBox() {
  const box = document.getElementById("mods-box");
  if (!box) return;
  const items = (currentState && currentState.mods) || [];
  box.style.display = items.length ? "block" : "none";
  document.getElementById("mods-count").textContent = items.length;
  document.getElementById("mods-list").innerHTML = items
    .map(
      (m) => `
      <div class="mod-row">
        <span class="mod-kind">${escapeHtml(m.kind)}</span>
        <span class="mod-label" title="mods/${escapeHtml(m.path)}">${escapeHtml(m.label)}</span>
        <button class="ghost-sm" data-path="${escapeHtml(m.path)}" onclick="removeModStep(this)">移除</button>
      </div>`
    )
    .join("");
}

async function removeModStep(btn) {
  if (btn.dataset.armed !== "1") {
    btn.dataset.armed = "1";
    btn.textContent = "确认移除";
    return;
  }
  const res = await fetch("/api/mods/remove", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: btn.dataset.path }),
  });
  const data = await res.json();
  if (!res.ok) return showError(data.detail || "移除失败");
  currentState = data.hud;
  renderAll();
  showToast("已移除这项定制");
}

function renderTunerDock() {
  renderModsBox();
  const box = document.getElementById("tuner-messages");
  if (!box) return;
  const hist = (currentState && currentState.tuner_history) || [];
  if (!hist.length) {
    box.innerHTML = `
      <div class="tuner-empty-hint">
        想改界面、文风、设定，或者想加新玩法，直接在下面说。
      </div>`;
    return;
  }

  box.innerHTML = hist
    .map((m) => {
      const roleCls = m.role === "user" ? "user" : "assistant";
      const who = m.role === "user" ? "你（场外诉求）" : "🎛️ Tuner Sub-Agent";
      const receipts = (m.actions || [])
        .map((a) => `<div class="receipt-pill">✓ ${escapeHtml(a.summary || "")}</div>`)
        .join("");
      return `
        <div class="tuner-msg ${roleCls}">
          <div style="font-size:0.71rem;color:#8b9bb4;margin-bottom:3px;display:flex;justify-content:space-between;">
            <span>${who}</span><span>${escapeHtml(m.time || "")}</span>
          </div>
          <div style="white-space:pre-wrap;">${escapeHtml(m.content || "")}</div>
          ${receipts ? `<div class="tuner-action-receipts">${receipts}</div>` : ""}
        </div>`;
    })
    .join("");
  box.scrollTop = box.scrollHeight;
}

function fillTunerPrompt(text) {
  const el = document.getElementById("tuner-input");
  if (!el) return;
  el.value = text;
  el.focus();
}

// ==================== SSE 流式消费器、自适应打字机与无剧透实时状态控制 ====================

async function consumeSsePost(url, payload, handlers = {}, signal = null) {
  const fetchOpts = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  };
  if (signal) fetchOpts.signal = signal;
  const res = await fetch(url, fetchOpts);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const errJson = await res.json();
      detail = errJson.detail || JSON.stringify(errJson);
    } catch (_) {}
    throw new Error(detail);
  }
  if (!res.body || !res.body.getReader) {
    throw new Error("当前浏览器环境不支持可读流");
  }

  const reader = res.body.getReader();
  if (signal) {
    signal.addEventListener("abort", () => {
      try {
        reader.cancel();
      } catch (_) {}
    }, { once: true });
  }
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let finalResult = null;

  const processBlock = (block) => {
    const lines = block.split(/\r?\n/);
    const dataLines = [];
    for (const ln of lines) {
      if (ln.startsWith("data:")) {
        dataLines.push(ln.slice(5).trimStart());
      }
    }
    if (!dataLines.length) return;
    const rawJson = dataLines.join("\n").trim();
    if (!rawJson || rawJson === "[DONE]") return;

    let ev;
    try {
      ev = JSON.parse(rawJson);
    } catch (_) {
      return;
    }

    if (ev.type === "status") {
      if (handlers.onStatus) handlers.onStatus(ev);
    } else if (ev.type === "delta") {
      if (handlers.onDelta) handlers.onDelta(ev.text || "");
    } else if (ev.type === "step_reset") {
      if (handlers.onStepReset) handlers.onStepReset();
    } else if (ev.type === "done") {
      finalResult = ev.result;
      if (handlers.onDone) handlers.onDone(ev.result, ev.trace_id);
    } else if (ev.type === "error") {
      if (ev.trace_id) console.error(`[AI-RPG Trace ${ev.trace_id}]`, ev.detail);
      const err = new Error(ev.detail || "流式执行异常");
      if (ev.cancelled) err.name = "AbortError";
      throw err;
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sepIdx;
    while ((sepIdx = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, sepIdx);
      buffer = buffer.slice(sepIdx + 2);
      processBlock(block);
    }
  }
  buffer += decoder.decode();
  if (buffer.trim()) {
    processBlock(buffer);
  }
  if (finalResult == null) throw new Error("连接中断，未收到任务完成结果");
  return finalResult;
}

function createSmoothTypewriter(onRender) {
  let fullText = "";
  let shownLen = 0;
  let rafId = null;

  const stepFrame = () => {
    const backlog = fullText.length - shownLen;
    if (backlog <= 0) {
      rafId = null;
      onRender(fullText, true);
      return;
    }
    const stride = Math.max(1, Math.ceil(backlog / 7));
    shownLen = Math.min(fullText.length, shownLen + stride);
    onRender(fullText.slice(0, shownLen), true);
    if (shownLen < fullText.length) {
      rafId = requestAnimationFrame(stepFrame);
    } else {
      rafId = null;
    }
  };

  return {
    push(chunk) {
      if (!chunk) return;
      fullText += chunk;
      if (rafId === null) {
        rafId = requestAnimationFrame(stepFrame);
      }
    },
    reset() {
      if (rafId !== null) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
      fullText = "";
      shownLen = 0;
      onRender("", false);
    },
    finish() {
      if (rafId !== null) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
      shownLen = fullText.length;
      onRender(fullText, false);
    },
    hasText() {
      return fullText.length > 0;
    },
  };
}

function setLiveStatusBar(label, isWorldSim = false) {
  const bar = document.getElementById("live-status-bar");
  const lbl = document.getElementById("live-status-label");
  if (!bar || !lbl) return;
  if (!label) {
    bar.style.display = "none";
    bar.classList.remove("world-sim-mode");
    return;
  }
  bar.style.display = "flex";
  bar.classList.toggle("world-sim-mode", Boolean(isWorldSim));
  lbl.textContent = label;
}

function stripInlineOocPreview(rawInput) {
  const cleaned = String(rawInput || "")
    .replace(/[（(【\[]\s*(?:OOC|ooc|场外|导演|微调|吐槽|提示|注意|要求|补充|备注)\s*[：:]?\s*[^）)】\]]+?\s*[）)】\]]/g, "")
    .trim();
  return cleaned || String(rawInput || "").trim();
}

async function submitTunerChat() {
  const el = document.getElementById("tuner-input");
  const msg = el ? el.value.trim() : "";
  if (!msg) return showError("请输入需要调教或定制的内容");

  const btn = document.getElementById("tuner-send-btn");
  const box = document.getElementById("tuner-messages");
  btn.disabled = true;
  btn.textContent = "调教执行中…";
  // 发出去就清空，没成功再放回去（和主输入框一样）
  el.value = "";

  // 插入即时用户气泡与按需流式助手气泡（仅展示无剧透阶段状态与最终回复）
  let liveReplyNode = null;
  let liveStatusNode = null;
  if (box) {
    const emptyHint = box.querySelector(".tuner-empty-hint");
    if (emptyHint) emptyHint.remove();
    const userDiv = document.createElement("div");
    userDiv.className = "tuner-msg user";
    userDiv.innerHTML = `<div style="font-size:0.71rem;color:#8b9bb4;margin-bottom:3px;"><span>你（场外诉求）</span></div><div style="white-space:pre-wrap;">${escapeHtml(msg)}</div>`;
    box.appendChild(userDiv);

    const astDiv = document.createElement("div");
    astDiv.className = "tuner-msg assistant";
    astDiv.innerHTML = `
      <div style="font-size:0.71rem;color:#8b9bb4;margin-bottom:3px;"><span>🎛️ Tuner Sub-Agent</span></div>
      <div class="streaming-placeholder" style="padding:2px 0;font-size:0.78rem;"><span class="live-pulse-dot"></span><span>正在构思定制方案…</span></div>
      <div style="white-space:pre-wrap;"></div>`;
    box.appendChild(astDiv);
    liveStatusNode = astDiv.querySelector(".streaming-placeholder span:last-child");
    liveReplyNode = astDiv.querySelector("div:last-child");
    box.scrollTop = box.scrollHeight;
  }

  const tw = createSmoothTypewriter((txt, showCaret) => {
    if (!liveReplyNode) return;
    if (txt && liveStatusNode && liveStatusNode.parentElement) {
      liveStatusNode.parentElement.style.display = "none";
    }
    liveReplyNode.innerHTML = escapeHtml(txt) + (showCaret && txt ? '<span class="typewriter-caret">▍</span>' : "");
    if (box) box.scrollTop = box.scrollHeight;
  });

  try {
    const data = await consumeSsePost(
      "/api/tuner/stream",
      { message: msg },
      {
        onStatus(ev) {
          if (liveStatusNode && !tw.hasText()) {
            liveStatusNode.textContent = ev.label || "正在执行场外调教…";
          }
        },
        onDelta(chunk) {
          tw.push(chunk);
        },
      }
    );
    tw.finish();
    if (data) {
      currentState = data.hud;
      if (data.ui_updated) {
        const themeSel = document.getElementById("theme-selector");
        if (themeSel) themeSel.value = "pack";
      }
      renderAll();
    }
  } catch (e) {
    if (!el.value.trim()) el.value = msg;
    renderTunerDock();
    showError("场外调教执行出错（你的内容已放回输入框）：" + describeFetchError(e));
  } finally {
    btn.disabled = false;
    btn.textContent = "唤醒调教助手";
  }
}

async function submitQuickNoteOnly() {
  const el = document.getElementById("tuner-input");
  const note = el ? el.value.trim() : "";
  if (!note) return showError("请先在上方输入框写下反馈笔记");
  el.value = "";
  try {
    const res = await fetch("/api/style/example", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        excerpt: "【场外快速试玩笔记】",
        liked: false,
        reason: note,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "没记上");
    showToast(`已记入 ${data.file} 与 engine/docs/playtest.md`, 4200);
  } catch (e) {
    if (!el.value.trim()) el.value = note;
    showError(`笔记没记上（内容已放回输入框）：${describeFetchError(e)}`);
  }
}

async function clearTunerChat() {
  await fetch("/api/tuner/clear", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  await fetchState();
}

// ==================== 四阶段初次启动自检与创世引导工作台 (System Self-Check & Genesis Onboarding) ====================

function applyApiPreset(prefix, presetId) {
  const p = getApiPresetsMap()[presetId];
  if (!p) return;
  const baseEl = document.getElementById(`${prefix}-api-base`);
  const modelEl = document.getElementById(`${prefix}-model`);
  const fmtEl = document.getElementById(`${prefix}-api-format`);
  if (baseEl) baseEl.value = p.api_base;
  if (modelEl) modelEl.value = p.model;
  if (fmtEl) fmtEl.value = p.api_format;
  syncBackModelCard(prefix, true);
}

// ---------- 幕后模型选择：只写入玩家配置 config.yaml，与世界包无关 ----------

function frontKindFor(prefix) {
  const type = prefix === "ob" ? currentObDriverType : (document.getElementById("cfg-front-type") || {}).value || "api";
  const cliTool = (document.getElementById(`${prefix}-cli-tool`) || {}).value || "claude";
  const harnessMode = (document.getElementById(`${prefix}-harness-mode`) || {}).value || "external_ide";
  return { type, cliTool, harnessMode };
}

function backModelSupported(prefix) {
  const { type, cliTool, harnessMode } = frontKindFor(prefix);
  return type === "api" || (type === "cli" && cliTool === "claude") || (type === "harness" && harnessMode === "openai_agents_sdk");
}

function recommendedBackModel(prefix) {
  const { type } = frontKindFor(prefix);
  if (type === "cli") return "opus";
  const base = ((document.getElementById(`${prefix}-api-base`) || {}).value || "").trim().replace(/\/$/, "");
  const preset = Object.values(getApiPresetsMap()).find((p) => (p.api_base || "").replace(/\/$/, "") === base);
  return (preset && preset.back_model) || "";
}

function renderBackModelCard(prefix) {
  const box = document.getElementById(`${prefix}-back-card`);
  if (!box) return;
  const cfg = (currentState && currentState.config) || {};
  const back = cfg.back_driver || {};
  // 第一次配置时默认选推荐项；玩家已经选过“同一个模型”就尊重它
  const chose_same = back.inherit_front !== false && !!cfg.driver_verified;
  const custom = back.inherit_front === false && back.same_endpoint === false;
  box.innerHTML = `
    <div class="back-model-title">幕后模型</div>
    <p class="back-model-desc">幕后负责推演世界和设计伏笔，只在世界推进时启动。</p>
    <div id="${prefix}-back-options">
      <label class="back-choice">
        <input type="radio" name="${prefix}-back-mode" id="${prefix}-back-strong" ${chose_same ? "" : "checked"} onchange="syncBackModelCard('${prefix}')" />
        <span>用更强的模型</span><span class="back-recommend">推荐</span>
      </label>
      <div class="back-strong-fields" id="${prefix}-back-fields">
        <div class="form-group">
          <label>模型</label>
          <input type="text" id="${prefix}-back-model" value="${escapeHtml(back.model || "")}" oninput="this.dataset.touched='1'" />
        </div>
        <label class="back-choice sub" id="${prefix}-back-custom-row">
          <input type="checkbox" id="${prefix}-back-custom" ${custom ? "checked" : ""} onchange="syncBackModelCard('${prefix}')" />
          <span>用另一家的接口</span>
        </label>
        <div id="${prefix}-back-custom-fields" style="display:none;">
          <div class="form-row-2">
            <div class="form-group">
              <label>API Base URL</label>
              <input type="text" id="${prefix}-back-api-base" value="${escapeHtml(back.api_base || "")}" placeholder="https://api.anthropic.com/v1" />
            </div>
            <div class="form-group">
              <label>协议类型</label>
              <select id="${prefix}-back-api-format">
                <option value="openai">OpenAI 通用兼容（推荐）</option>
                <option value="auto">自动识别</option>
                <option value="anthropic">Anthropic 原生</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label>API Key</label>
            <input type="password" id="${prefix}-back-api-key" placeholder="${back.has_saved_key ? "已保存密钥（留空保持不变）" : "这家接口的密钥"}" />
          </div>
        </div>
      </div>
      <label class="back-choice">
        <input type="radio" name="${prefix}-back-mode" id="${prefix}-back-same" ${chose_same ? "checked" : ""} onchange="syncBackModelCard('${prefix}')" />
        <span>和台前相同</span><span class="back-note-inline">更省钱</span>
      </label>
    </div>
    <p class="back-model-desc" id="${prefix}-back-unsupported" style="display:none;">这个工具不能单独指定幕后模型，幕后和台前用同一个模型。</p>`;
  const fmt = document.getElementById(`${prefix}-back-api-format`);
  if (fmt) fmt.value = back.api_format || "openai";
  syncBackModelCard(prefix);
}

function syncBackModelCard(prefix, presetChanged = false) {
  const options = document.getElementById(`${prefix}-back-options`);
  if (!options) return;
  const supported = backModelSupported(prefix);
  options.style.display = supported ? "block" : "none";
  document.getElementById(`${prefix}-back-unsupported`).style.display = supported ? "none" : "block";
  const strong = document.getElementById(`${prefix}-back-strong`).checked;
  document.getElementById(`${prefix}-back-fields`).style.display = strong ? "block" : "none";
  const isApi = frontKindFor(prefix).type === "api";
  document.getElementById(`${prefix}-back-custom-row`).style.display = isApi ? "flex" : "none";
  const custom = isApi && document.getElementById(`${prefix}-back-custom`).checked;
  document.getElementById(`${prefix}-back-custom-fields`).style.display = custom ? "block" : "none";
  const modelEl = document.getElementById(`${prefix}-back-model`);
  if (!custom && modelEl && modelEl.dataset.touched !== "1" && (presetChanged || !modelEl.value)) {
    modelEl.value = recommendedBackModel(prefix);
  }
}

function collectBackDriver(prefix) {
  if (!backModelSupported(prefix) || document.getElementById(`${prefix}-back-same`).checked) {
    return { inherit_front: true };
  }
  const model = document.getElementById(`${prefix}-back-model`).value.trim();
  if (!model) throw new Error("请填写幕后模型，或选“和台前相同”");
  if (!document.getElementById(`${prefix}-back-custom`).checked || frontKindFor(prefix).type !== "api") {
    return { inherit_front: false, same_endpoint: true, model };
  }
  const apiBase = document.getElementById(`${prefix}-back-api-base`).value.trim();
  if (!apiBase) throw new Error("请填写幕后接口的 API Base URL");
  return {
    inherit_front: false,
    same_endpoint: false,
    type: "api",
    model,
    api_base: apiBase,
    api_format: document.getElementById(`${prefix}-back-api-format`).value,
    api_key: document.getElementById(`${prefix}-back-api-key`).value.trim(),
  };
}

function selectOnboardingDriverType(dtype) {
  currentObDriverType = dtype || "api";
  ["api", "cli", "harness"].forEach((k) => {
    const tab = document.getElementById(`ob-dtype-${k}`);
    const pane = document.getElementById(`ob-driver-pane-${k}`);
    if (tab) tab.classList.toggle("active", k === currentObDriverType);
    if (pane) pane.style.display = k === currentObDriverType ? "block" : "none";
  });
  syncBackModelCard("ob", true);
}

function hydrateOnboardingDriverForm() {
  const cfg = (currentState && currentState.config) || {};
  const fd = cfg.front_driver || {};
  selectOnboardingDriverType(fd.type || "api");
  const fmtEl = document.getElementById("ob-api-format");
  const baseEl = document.getElementById("ob-api-base");
  const keyEl = document.getElementById("ob-api-key");
  const modelEl = document.getElementById("ob-model");
  const cliEl = document.getElementById("ob-cli-tool");
  const harEl = document.getElementById("ob-harness-mode");
  if (fmtEl) fmtEl.value = fd.api_format || "openai";
  if (baseEl) baseEl.value = fd.api_base || "https://api.deepseek.com/v1";
  if (keyEl) {
    keyEl.value = "";
    keyEl.placeholder = fd.has_saved_key ? "已保存密钥（留空保持不变，输入新值可覆盖）" : "sk-...";
  }
  if (modelEl) modelEl.value = fd.model || "deepseek-chat";
  if (cliEl) cliEl.value = fd.cli_tool || "claude";
  if (harEl) harEl.value = fd.harness_mode || "external_ide";
  renderBackModelCard("ob");
}

async function refreshSystemSelfCheck() {
  const strip = document.getElementById("genesis-selfcheck-strip");
  try {
    const res = await fetch("/api/system/selfcheck");
    const rep = await res.json();
    latestSelfCheck = rep;
    if (strip) {
      // 一切正常时不打扰玩家，只把有问题的项列出来
      const problems = (rep.checks || []).filter((c) => !c.ok);
      strip.innerHTML = problems
        .map((c) => `<span class="selfcheck-badge warn" title="${escapeHtml(c.detail || "")}">! ${escapeHtml(c.label)}</span>`)
        .join("");
      strip.style.display = problems.length ? "flex" : "none";
    }

    const cliBox = document.getElementById("ob-cli-detected-list");
    if (cliBox && Array.isArray(rep.clis)) {
      const installed = rep.clis.filter((c) => c.available);
      cliBox.innerHTML = `<div class="detected-note">${
        installed.length ? `这台电脑上有：${installed.map((c) => escapeHtml(c.name)).join("、")}` : "这台电脑上没有检测到这些工具，需要先安装。"
      }</div>`;
      const firstAvail = rep.clis.find((c) => c.available);
      const cliSel = document.getElementById("ob-cli-tool");
      if (firstAvail && cliSel && !((currentState?.config?.front_driver || {}).cli_tool)) {
        cliSel.value = firstAvail.id;
      }
    }

    const harBox = document.getElementById("ob-harness-detected-list");
    if (harBox && Array.isArray(rep.harnesses)) {
      const ready = rep.harnesses.filter((h) => h.available);
      harBox.innerHTML = `<div class="detected-note">${
        ready.length ? `已就绪：${ready.map((h) => escapeHtml(h.name)).join("、")}` : "这台电脑上还没有安装这些框架。"
      }</div>`;
    }
  } catch (e) {
    if (strip) {
      strip.innerHTML = `<span class="selfcheck-badge warn">! 环境检查失败：${escapeHtml(e.message)}</span>`;
      strip.style.display = "flex";
    }
  }
}

function openGenesisModal(step = null) {
  // 页面刚打开、状态还没取回来时先取状态，否则世界列表是空的
  if (!currentState) {
    fetchState()
      .then(() => openGenesisModal(step))
      .catch((e) => showError(describeFetchError(e)));
    return;
  }
  const ob = (currentState && currentState.onboarding) || {};
  const job = currentJob || (currentState && currentState.job);
  const targetStep = job ? JOB_STEPS[job.op] : step || ob.recommended_step || 1;
  hydrateOnboardingDriverForm();
  populateGenesisSelectors();
  switchGenesisStep(targetStep);
  refreshSystemSelfCheck();

  const drvAlert = document.getElementById("genesis-driver-alert");
  if (drvAlert) {
    if (ob.driver_ready === false && targetStep !== 1) {
      drvAlert.style.display = "flex";
      drvAlert.innerHTML = `
        <span>还没连接 AI，请先完成第 ① 步。</span>
        <button class="primary" onclick="switchGenesisStep(1)">去连接</button>`;
    } else {
      drvAlert.style.display = "none";
    }
  }

  const unpAlert = document.getElementById("genesis-unparsed-alert");
  if (unpAlert) {
    const unp = ob.unparsed_packs || [];
    if (unp.length) {
      const names = unp.map((u) => escapeHtml(u.name || u.id)).join("、");
      unpAlert.style.display = "flex";
      unpAlert.innerHTML = `
        <span>有待处理的设定文件夹：${names}。可以在第 ② 步让 AI 整理。</span>
        <button onclick="openPackBrowserModal()">查看</button>`;
    } else {
      unpAlert.style.display = "none";
    }
  }

  document.getElementById("genesis-result-box").style.display = "none";
  document.getElementById("genesis-modal").style.display = "flex";
}

// ---------- 世界包开局设置：按 pack.yaml 的 setup 清单动态提问 ----------

function findPack(packId) {
  return ((currentState && currentState.packs) || []).find((p) => p.id === packId) || null;
}

function renderSetupFields(prefix, packId) {
  const box = document.getElementById(`${prefix}-setup-fields`);
  if (!box) return;
  const items = (findPack(packId) || {}).setup || [];
  box.innerHTML = items
    .map((item) => {
      const fieldId = escapeHtml(`${prefix}-setup-${item.id}`);
      const value = escapeHtml(item.value || "");
      const placeholder = escapeHtml(item.hint || (item.required ? "" : "留空交给 AI 决定"));
      let control;
      if (item.type === "choice") {
        const blank = item.required ? `<option value="">请选择</option>` : `<option value="">交给 AI 决定</option>`;
        const options = item.options
          .map((o) => `<option value="${escapeHtml(o)}" ${o === item.value ? "selected" : ""}>${escapeHtml(o)}</option>`)
          .join("");
        control = `<select id="${fieldId}">${blank}${options}</select>`;
      } else if (item.type === "textarea") {
        control = `<textarea id="${fieldId}" rows="2" placeholder="${placeholder}">${value}</textarea>`;
      } else {
        control = `<input type="text" id="${fieldId}" value="${value}" placeholder="${placeholder}" />`;
      }
      return `<div class="form-group"><label>${escapeHtml(item.label)}${item.required ? "" : '<span class="hud-hint">选填</span>'}</label>${control}</div>`;
    })
    .join("");
  const prefLabel = document.getElementById(`${prefix}-pref-label`);
  if (prefLabel) {
    prefLabel.textContent = items.length ? "其他想法（选填）" : "主角和开局的想法（选填）";
  }
}

function collectSetupAnswers(prefix, packId) {
  const answers = {};
  const missing = [];
  ((findPack(packId) || {}).setup || []).forEach((item) => {
    const el = document.getElementById(`${prefix}-setup-${item.id}`);
    const value = el ? el.value.trim() : "";
    if (value) answers[item.id] = value;
    else if (item.required) missing.push(item.label);
  });
  if (missing.length) throw new Error(`开局还需要填写：${missing.join("、")}`);
  return answers;
}

function pickWorldPack(packId) {
  const sel = document.getElementById("gen-seed-pack");
  if (sel) {
    sel.value = packId;
    onSeedPackChange();
  }
  switchGenesisStep(3);
}

function populateGenesisSelectors() {
  const packs = (currentState && currentState.packs) || [];
  const readyPacks = packs.filter((p) => p.status === "ready");
  const unparsedPacks = packs.filter((p) => p.status !== "ready");

  const readyBanner = document.getElementById("step2-ready-packs-banner");
  if (readyBanner) {
    readyBanner.style.display = readyPacks.length ? "flex" : "none";
    readyBanner.innerHTML = readyPacks
      .map(
        (p) => `
        <button type="button" class="pack-pick" onclick="pickWorldPack(${escapeHtml(JSON.stringify(p.id))})">
          <span class="pack-pick-name">${escapeHtml(p.name)}</span>
          <span class="pack-pick-desc">${escapeHtml(p.description || p.genre || "")}</span>
        </button>`
      )
      .join("");
  }
  const newWorld = document.getElementById("gen-new-world");
  if (newWorld && !readyPacks.length) newWorld.open = true;
  const sourceRow = document.getElementById("gen-source-row");
  if (sourceRow) sourceRow.style.display = unparsedPacks.length ? "flex" : "none";

  const srcSel = document.getElementById("gen-source-pack");
  if (srcSel) {
    srcSel.innerHTML = `<option value="">— 从零原创构想 —</option>`;
    unparsedPacks.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = `${p.id}（${p.raw_files.length} 个文件）`;
      srcSel.appendChild(opt);
    });
    srcSel.onchange = () => {
      if (srcSel.value && !document.getElementById("gen-pack-id").value.trim()) {
        document.getElementById("gen-pack-id").value = srcSel.value;
      }
    };
  }

  ["gen-seed-pack", "gen-ui-pack"].forEach((selId) => {
    const el = document.getElementById(selId);
    if (!el) return;
    el.innerHTML = "";
    if (!readyPacks.length) {
      el.innerHTML = `<option value="">（请先在第 ② 步拖入或生成世界规则包）</option>`;
      return;
    }
    readyPacks.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.name;
      if (p.id === currentState.pack_id) opt.selected = true;
      el.appendChild(opt);
    });
  });
  const seedSel = document.getElementById("gen-seed-pack");
  const nameInput = document.getElementById("gen-world-name");
  const counts = (currentState && currentState.entity_counts) || {};
  const emptySave =
    currentState &&
    currentState.has_save &&
    !(currentState.story_turns || []).length &&
    !((counts.npcs || 0) + (counts.locations || 0));
  if (emptySave && nameInput && !nameInput.value) nameInput.value = currentState.save_name || "";
  if (seedSel) onSeedPackChange();
}

function switchGenesisStep(step) {
  currentGenesisStep = step;
  const resultBox = document.getElementById("genesis-result-box");
  if (resultBox) resultBox.style.display = "none";
  applyGenesisStepView();
  const drvAlert = document.getElementById("genesis-driver-alert");
  if (drvAlert && step === 1) {
    drvAlert.style.display = "none";
  }
}

// 向导同一时间只显示一样东西：任务进度、结果，或者当前步骤的表单
function applyGenesisStepView() {
  const lockedStep = currentJob ? JOB_STEPS[currentJob.op] : 0;
  if (lockedStep) currentGenesisStep = lockedStep;
  const resultBox = document.getElementById("genesis-result-box");
  const showingResult = !lockedStep && resultBox && resultBox.style.display !== "none";
  if (lockedStep && resultBox) resultBox.style.display = "none";
  [1, 2, 3, 4].forEach((n) => {
    const p = document.getElementById(`genesis-step-${n}`);
    const b = document.getElementById(`gstep-btn-${n}`);
    if (p) p.style.display = !lockedStep && !showingResult && n === currentGenesisStep ? "block" : "none";
    if (b) {
      b.classList.toggle("active", n === currentGenesisStep);
      b.disabled = Boolean(lockedStep) && n !== lockedStep;
      b.title = b.disabled ? `「${currentJob.label}」进行中，完成后才能切换步骤` : "";
    }
  });
}

async function verifyOnboardingDriverAndNext() {
  const btn = document.getElementById("gen-btn-1");
  const st = document.getElementById("gen-status-1");
  btn.disabled = true;
  st.textContent = "正在测试连接…";

  let backDriver;
  try {
    backDriver = collectBackDriver("ob");
  } catch (e) {
    st.textContent = `✗ ${e.message}`;
    btn.disabled = false;
    return;
  }

  const candidate = {
    type: currentObDriverType,
    api_format: document.getElementById("ob-api-format").value,
    api_base: document.getElementById("ob-api-base").value.trim(),
    api_key: document.getElementById("ob-api-key").value.trim(),
    model: document.getElementById("ob-model").value.trim(),
    cli_tool: document.getElementById("ob-cli-tool").value,
    harness_mode: document.getElementById("ob-harness-mode").value,
  };

  try {
    const res = await fetch("/api/driver/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ front_driver: candidate, back_driver: backDriver }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "连接失败");

    currentState = data.hud;
    renderAll();
    await refreshSystemSelfCheck();

    if (data.back_check && !data.back_check.ok) {
      st.textContent = `幕后模型连接失败：${data.back_check.detail}。改一下模型名，或选“和台前相同”。`;
      return;
    }

    const chk = data.check || {};
    if (chk.web_execution === false) {
      st.textContent = chk.detail;
      return;
    }
    st.textContent = "";
    switchGenesisStep(2);
  } catch (e) {
    st.textContent = `连接失败：${e.message}`;
      } finally {
    btn.disabled = false;
  }
}

// ---------- 阶段二 A：浏览器直接拖入 .rpgpack / .zip / 设定文件夹 ----------

function handlePackDragOver(ev) {
  ev.preventDefault();
  ev.stopPropagation();
  const dz = document.getElementById("pack-dropzone");
  if (dz) dz.classList.add("drag-active");
}

function handlePackDragLeave(ev) {
  ev.preventDefault();
  ev.stopPropagation();
  const dz = document.getElementById("pack-dropzone");
  if (dz) dz.classList.remove("drag-active");
}

function _readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const s = String(reader.result || "");
      const idx = s.indexOf(",");
      resolve(idx >= 0 ? s.slice(idx + 1) : s);
    };
    reader.onerror = () => reject(new Error("读取压缩包失败"));
    reader.readAsDataURL(file);
  });
}

function _readFileAsText(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error(`读取文本文件失败: ${file.name}`));
    reader.readAsText(file, "utf-8");
  });
}

async function _collectDroppedEntries(dataTransfer) {
  const items = Array.from(dataTransfer.items || []);
  if (items.length && typeof items[0].webkitGetAsEntry === "function") {
    const collected = [];
    let topDirName = "";
    const walkEntry = (entry, prefix = "") =>
      new Promise((resolve) => {
        if (!entry) return resolve();
        if (entry.isFile) {
          entry.file(
            (file) => {
              collected.push({ file, relPath: prefix ? `${prefix}/${file.name}` : file.name });
              resolve();
            },
            () => resolve()
          );
        } else if (entry.isDirectory) {
          if (!prefix && !topDirName) topDirName = entry.name;
          const dirReader = entry.createReader();
          const readBatch = () => {
            dirReader.readEntries(async (entries) => {
              if (!entries.length) return resolve();
              for (const child of entries) {
                await walkEntry(child, prefix ? `${prefix}/${entry.name}` : entry.name);
              }
              readBatch();
            }, () => resolve());
          };
          readBatch();
        } else {
          resolve();
        }
      });
    for (const it of items) {
      await walkEntry(it.webkitGetAsEntry(), "");
    }
    return { topDirName, collected };
  }

  const rawFiles = Array.from(dataTransfer.files || []);
  return {
    topDirName: "",
    collected: rawFiles.map((f) => ({ file: f, relPath: f.webkitRelativePath || f.name })),
  };
}

async function handlePackDrop(ev) {
  ev.preventDefault();
  ev.stopPropagation();
  handlePackDragLeave(ev);
  const { topDirName, collected } = await _collectDroppedEntries(ev.dataTransfer);
  if (!collected.length) return;

  if (collected.length === 1 && /\.(rpgpack|zip)$/i.test(collected[0].file.name)) {
    await uploadArchiveFile(collected[0].file);
  } else {
    await uploadFolderFiles(collected, topDirName);
  }
}

async function handleArchiveInputSelect(ev) {
  const f = ev.target.files && ev.target.files[0];
  ev.target.value = "";
  if (f) await uploadArchiveFile(f);
}

async function handleFolderInputSelect(ev) {
  const rawFiles = Array.from(ev.target.files || []);
  ev.target.value = "";
  if (!rawFiles.length) return;
  let topDir = "";
  if (rawFiles[0].webkitRelativePath) {
    topDir = rawFiles[0].webkitRelativePath.split("/")[0] || "";
  }
  const collected = rawFiles.map((f) => ({ file: f, relPath: f.webkitRelativePath || f.name }));
  await uploadFolderFiles(collected, topDir);
}

async function uploadArchiveFile(file) {
  const st = document.getElementById("pack-drop-status");
  if (st) st.textContent = `正在导入 ${file.name}…`;
  try {
    const b64 = await _readFileAsBase64(file);
    await submitPackUploadPayload({ filename: file.name, data_base64: b64 });
  } catch (e) {
    if (st) st.textContent = `✗ 导入失败：${e.message}`;
    showError("导入世界包失败：" + e.message);
  }
}

async function uploadFolderFiles(collected, topDirName = "") {
  const st = document.getElementById("pack-drop-status");
  const textAllowed = /\.(md|txt|yaml|yml|json)$/i;
  const filtered = collected.filter((x) => textAllowed.test(x.file.name));
  if (!filtered.length) {
    showError("没有找到可以导入的文件");
    return;
  }
  const packId = topDirName || filtered[0].file.name.replace(/\.[^.]+$/, "") || "imported_pack";
  if (st) st.textContent = `⏳ 正在导入文件夹 ${packId}（共 ${filtered.length} 个设定文件）并执行体检…`;

  try {
    const filesPayload = [];
    for (const item of filtered) {
      let rel = item.relPath.replace(/\\/g, "/");
      if (topDirName && rel.startsWith(`${topDirName}/`)) {
        rel = rel.slice(topDirName.length + 1);
      }
      const content = await _readFileAsText(item.file);
      filesPayload.push({ path: rel, content });
    }
    await submitPackUploadPayload({ pack_id: packId, files: filesPayload });
  } catch (e) {
    if (st) st.textContent = `✗ 导入失败：${e.message}`;
    showError("导入设定文件夹失败：" + e.message);
  }
}

async function submitPackUploadPayload(payload) {
  const st = document.getElementById("pack-drop-status");
  const res = await fetch("/api/packs/import_upload", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "导入体检失败");

  if (data.hud) {
    currentState = data.hud;
    const themeSel = document.getElementById("theme-selector");
    if (themeSel) themeSel.value = "pack";
    renderAll();
  }
  populateGenesisSelectors();
  if (document.getElementById("pack-browser-modal").style.display === "flex") {
    await refreshPackBrowser(data.id);
  }

  const resBox = document.getElementById("genesis-result-box");
  resBox.style.display = "block";

  if (data.import_kind === "seeded_world") {
    if (st) st.textContent = "";
    resBox.innerHTML = `
      <h4><span>已导入世界「${escapeHtml(data.id)}」，可以直接玩</span></h4>
      <div style="display:flex;justify-content:flex-end;gap:8px;">
        <button onclick="switchGenesisStep(4)">定制界面</button>
        <button class="primary" onclick="closeModal('genesis-modal')">进入游戏</button>
      </div>`;
    currentGenesisStep = 4;
    applyGenesisStepView();
  } else if (data.status === "ready") {
    if (st) st.textContent = "";
    document.getElementById("gen-seed-pack").value = data.id;
    document.getElementById("gen-ui-pack").value = data.id;
    if (!document.getElementById("gen-world-name").value.trim()) {
      document.getElementById("gen-world-name").value = `${data.id}_主世界`;
    }
    resBox.innerHTML = `
      <h4><span>世界包导入成功</span></h4>
      <div style="display:flex;justify-content:flex-end;">
        <button class="primary" onclick="switchGenesisStep(3)">下一步</button>
      </div>`;
    renderSeedFormHint();
    currentGenesisStep = 3;
    applyGenesisStepView();
  } else {
    // 接下来要填表，不显示结果框（结果框和表单不同时出现）
    if (st) st.textContent = "设定已导入，已选中这个文件夹。补充一句你的偏好后，点「设计新世界」让 AI 整理成世界包。";
    resBox.style.display = "none";
    const newWorldBox = document.getElementById("gen-new-world");
    if (newWorldBox) newWorldBox.open = true;
    const srcSel = document.getElementById("gen-source-pack");
    if (srcSel) srcSel.value = data.id;
    document.getElementById("gen-pack-id").value = data.id;
  }
}

// ---------- 阶段二 B / 阶段三 / 阶段四：AI 创世流水线 ----------

function fillGenesisPreset(pid, concept) {
  document.getElementById("gen-pack-id").value = pid;
  document.getElementById("gen-concept").value = concept;
}

function fillUiStylePreset(prefText) {
  const el = document.getElementById("gen-ui-pref");
  if (el) {
    el.value = prefText;
    el.focus();
  }
}

// ---------- 确认框：取消长任务、丢弃内容这类不能撤销的操作先问一句 ----------
function askConfirm({ title, body, yes, no }) {
  const modal = document.getElementById("confirm-modal");
  const yesBtn = document.getElementById("confirm-yes");
  const noBtn = document.getElementById("confirm-no");
  document.getElementById("confirm-title").textContent = title;
  document.getElementById("confirm-body").textContent = body;
  yesBtn.textContent = yes;
  noBtn.textContent = no;
  modal.style.display = "flex";
  return new Promise((resolve) => {
    const finish = (answer) => {
      modal.style.display = "none";
      yesBtn.onclick = null;
      noBtn.onclick = null;
      resolve(answer);
    };
    yesBtn.onclick = () => finish(true);
    noBtn.onclick = () => finish(false);
  });
}

function describeFetchError(e) {
  // 浏览器只在请求根本发不出去时抛 TypeError（"Failed to fetch"）
  return e instanceof TypeError ? "连不上游戏服务，请确认启动窗口还开着。" : e.message;
}

async function cancelActiveGenesisStream() {
  const job = currentJob;
  if (!job) return;
  const consequence = job.replaces
    ? "这次新生成的内容会留着，之后可以继续生成或放弃。旧世界不受影响。"
    : job.op === "seed"
    ? "已经生成的内容会保留，之后可以接着生成。"
    : job.op === "customize_ui"
    ? "界面可能只改了一部分。"
    : "世界设计可能不完整，需要重新设计。";
  const ok = await askConfirm({ title: `要取消「${job.label}」吗？`, body: consequence, yes: "取消任务", no: "继续运行" });
  if (!ok || !currentJob) return;
  jobCancelling = true;
  renderJobState(currentJob);
  const ctrl = activeGenesisAbortCtrl;
  requestBackendStreamCancel().finally(() => {
    if (ctrl) ctrl.abort();
  });
}

// ---------- 长任务：进度和结果都以服务器为准，切换步骤、刷新、关掉页面再回来看到的都一样 ----------
const JOB_STEPS = { conceive: 2, seed: 3, customize_ui: 4 };
const JOB_LABELS = { conceive: "设计世界", seed: "生成世界", customize_ui: "定制界面" };
const JOB_SEEN_KEY = "rpg.lastJobSeen";
let jobPollTimer = null;
let lastSeenJobFinish = readLastSeenJob();
let jobProblem = null;
let currentJob = null;
let jobCancelling = false;
let jobPollFailures = 0;
let retrySeedTarget = null;

function readLastSeenJob() {
  try {
    return Number(window.localStorage.getItem(JOB_SEEN_KEY)) || 0;
  } catch (_) {
    return 0;
  }
}

function rememberJobSeen(finishedAt) {
  lastSeenJobFinish = finishedAt;
  try {
    window.localStorage.setItem(JOB_SEEN_KEY, String(finishedAt));
  } catch (_) {}
}

function formatElapsed(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m} 分 ${s} 秒` : `${s} 秒`;
}

function isGenesisOpen() {
  return document.getElementById("genesis-modal").style.display === "flex";
}

function describeJob(job) {
  if (job.replaces) return `正在重新生成「${job.world}」`;
  return job.world ? `正在${job.label}「${job.world}」` : `正在${job.label}`;
}

function renderJobState(job) {
  currentJob = job || null;
  const banner = document.getElementById("genesis-job-banner");
  const liveCancel = document.getElementById("live-status-cancel");
  const running = Boolean(job);
  if (!running) jobCancelling = false;
  ["gen-btn-1", "gen-btn-2", "gen-btn-4"].forEach((id) => {
    const btn = document.getElementById(id);
    if (btn) btn.disabled = running;
  });
  const sendBtn = document.getElementById("send-btn");
  if (sendBtn && !activeTurnAbortCtrl) sendBtn.disabled = running;
  const saveSel = document.getElementById("save-selector");
  if (saveSel) saveSel.disabled = running;
  renderPendingWorlds();
  applyGenesisStepView();

  if (running) {
    const c = job.counts || {};
    const made = Object.keys(c).length ? ` · 势力 ${c.factions || 0} · 地点 ${c.locations || 0} · 人物 ${c.npcs || 0}` : "";
    const text = jobCancelling ? "正在取消…" : `${describeJob(job)}… 已用 ${formatElapsed(job.elapsed || 0)}${made}`;
    const notes = [];
    if (jobCancelling) notes.push("AI 停下来要几秒钟。");
    else {
      if (job.phase) notes.push(job.phase);
      notes.push(job.replaces ? "旧世界不会被改动，完成后由你决定用哪个。可以关掉这个窗口。" : "可以关掉这个窗口，任务不会中断。");
    }
    if (banner) {
      banner.className = "job-banner";
      banner.style.display = "flex";
      banner.innerHTML = `<span class="live-pulse-dot"></span><span class="job-banner-text">${escapeHtml(text)}<span class="job-banner-note">${escapeHtml(notes.join(" "))}</span></span><button type="button" ${jobCancelling ? "disabled" : ""} onclick="cancelActiveGenesisStream()">取消</button>`;
    }
    setLiveStatusBar(text, false);
    if (liveCancel) {
      liveCancel.style.display = "inline-block";
      liveCancel.disabled = jobCancelling;
    }
    return;
  }
  if (liveCancel) liveCancel.style.display = "none";
  if (banner) {
    if (jobProblem) {
      banner.className = "job-banner problem";
      banner.style.display = "flex";
      banner.innerHTML = `<span class="job-banner-text">「${escapeHtml(jobProblem.label)}」没有完成：${escapeHtml(jobProblem.error)}</span>${
        jobProblem.op === "seed" ? `<button type="button" onclick="retryFailedSeed()">继续生成</button>` : ""
      }<button type="button" onclick="dismissJobProblem()">知道了</button>`;
    } else {
      banner.style.display = "none";
    }
  }
}

function dismissJobProblem() {
  jobProblem = null;
  renderJobState(null);
}

// 继续生成不用重新填表：上次的开局回答和想法都存在存档里
function resumeSeed(worldName, packId) {
  jobProblem = null;
  openGenesisModal(3);
  return startGenesisJob(
    { stage: "seed", pack_id: packId, world_name: worldName, protagonist_pref: "", setup_answers: {} },
    "生成世界失败"
  );
}

function retryInterruptedSeed() {
  if (currentState) resumeSeed(currentState.save_name, currentState.pack_id);
}

function retryFailedSeed() {
  if (retrySeedTarget) resumeSeed(retrySeedTarget.world, retrySeedTarget.pack);
}

async function pollJobState() {
  let data;
  try {
    data = await (await fetch("/api/job")).json();
  } catch (_) {
    jobPollFailures += 1;
    if (currentJob && jobPollFailures >= 2) {
      // 服务器没了，任务也就没了：别让向导一直停在“正在运行”
      jobProblem = { label: currentJob.label, op: "", error: "连不上游戏服务，任务可能已经中断。请确认启动窗口还开着，重新启动后再试，已有的内容不会丢。" };
      clearInterval(jobPollTimer);
      jobPollTimer = null;
      setLiveStatusBar("", false);
      renderJobState(null);
    }
    return;
  }
  jobPollFailures = 0;
  const last = data.last;
  let finished = null;
  if (last && last.finished_at > lastSeenJobFinish) {
    rememberJobSeen(last.finished_at);
    finished = last;
    try {
      await fetchState();
    } catch (_) {}
  }
  renderJobState(data.job);
  if (finished && !data.job) renderJobOutcome(finished);
  if (data.job && !jobPollTimer) {
    jobPollTimer = setInterval(pollJobState, 3000);
  } else if (!data.job && jobPollTimer) {
    clearInterval(jobPollTimer);
    jobPollTimer = null;
    setLiveStatusBar("", false);
  }
}

async function startGenesisJob(payload, failLabel) {
  const seenBefore = lastSeenJobFinish;
  document.getElementById("genesis-result-box").style.display = "none";
  jobProblem = null;
  // 先按“已开始”显示，避免连点；服务器拒绝的话下面的轮询会恢复
  renderJobState({ op: payload.stage, label: JOB_LABELS[payload.stage], world: payload.world_name || "", elapsed: 0, counts: {} });
  activeGenesisAbortCtrl = new AbortController();
  setTimeout(pollJobState, 400);
  let failure = null;
  try {
    await consumeSsePost("/api/genesis/stream", payload, {}, activeGenesisAbortCtrl.signal);
  } catch (e) {
    failure = e;
  }
  activeGenesisAbortCtrl = null;
  await pollJobState();
  // 任务跑过的话结果已经由轮询显示；这里只提示任务根本没开始的情况
  if (failure && failure.name !== "AbortError" && !currentJob && lastSeenJobFinish === seenBefore) {
    showError(`${failLabel}：${describeFetchError(failure)}`);
  }
}

function showGenesisResult(html, closedMessage) {
  const box = document.getElementById("genesis-result-box");
  box.innerHTML = html;
  box.style.display = "block";
  applyGenesisStepView();
  if (!isGenesisOpen() && closedMessage) showToast(closedMessage, 6000);
}

function renderJobOutcome(last) {
  const r = last.result;
  if (last.cancelled) {
    showToast(last.replaces ? "已取消，旧世界没有变化。" : "已取消。");
    return;
  }
  if (!r) {
    retrySeedTarget = { world: last.world, pack: last.pack };
    if (!last.replaces) jobProblem = last; // 重新生成的失败由等待决定的卡片显示
    renderJobState(null);
    if (!isGenesisOpen() || last.replaces) showError(`「${last.label}」没有完成：${last.error}`);
    return;
  }
  if (last.op === "conceive") renderConceiveOutcome(last, r);
  else if (last.op === "seed") renderSeedOutcome(last, r);
  else renderCustomizeOutcome();
}

function renderConceiveOutcome(last, r) {
  populateGenesisSelectors();
  if (!last.ok) {
    const errs = (r.errors || []).map((e) => `<li>${escapeHtml(e)}</li>`).join("");
    showGenesisResult(
      `<h4 style="color:var(--accent-warn);"><span>世界设计好了，但还有问题需要修正</span></h4>
       ${errs ? `<ul style="color:var(--accent-danger);font-size:0.8rem;padding-left:18px;margin-bottom:8px;">${errs}</ul>` : ""}
       <div class="md-view" style="max-height:130px;overflow-y:auto;margin-bottom:8px;">${escapeHtml(r.summary || "")}</div>
       <div class="genesis-actions">
         <button onclick="openPackBrowserModal()">打开世界包中心检查</button>
         <button class="primary" onclick="switchGenesisStep(2)">回去修改</button>
       </div>`,
      "世界设计好了，但还有问题需要修正。"
    );
    return;
  }
  document.getElementById("gen-ui-pack").value = r.pack_id;
  const nameEl = document.getElementById("gen-world-name");
  if (!nameEl.value.trim()) nameEl.value = `${r.pack_id}_主世界`;
  showGenesisResult(
    `<h4><span>世界设计好了</span></h4>
     <div class="genesis-actions">
       <button class="primary" onclick="pickWorldPack(${escapeHtml(JSON.stringify(r.pack_id))})">下一步：生成世界</button>
     </div>`,
    "世界设计好了，打开开局向导继续。"
  );
}

function clearSeedForm() {
  document.getElementById("gen-world-name").value = "";
  document.getElementById("gen-protagonist-pref").value = "";
  renderSeedFormHint();
}

function renderSeedOutcome(last, r) {
  const c = r.counts || {};
  const made = `势力 ${c.factions || 0} · 地点 ${c.locations || 0} · 人物 ${c.npcs || 0}`;
  document.getElementById("gen-ui-pack").value = r.pack_id;
  retrySeedTarget = { world: last.world, pack: r.pack_id };

  if (r.replaces) {
    // 等玩家决定：主页面和向导顶部都有卡片
    clearSeedForm();
    if (last.ok) showToast(`「${r.replaces}」的新版本生成好了，选一下用哪个。`, 6000);
    else showError(`「${r.replaces}」的新版本有些内容没通过检查，可以继续生成。`);
    return;
  }
  if (!last.ok) {
    const errs = (r.errors || []).map((e) => `<li>${escapeHtml(e)}</li>`).join("");
    showGenesisResult(
      `<h4 style="color:var(--accent-warn);"><span>世界生成了，但有些内容没通过检查</span></h4>
       <div style="font-size:0.8rem;color:var(--text-muted);margin-bottom:6px;">${made}</div>
       ${errs ? `<ul style="color:var(--accent-danger);font-size:0.8rem;padding-left:18px;margin-bottom:8px;">${errs}</ul>` : ""}
       <div class="md-view" style="max-height:120px;overflow-y:auto;margin-bottom:8px;">${escapeHtml(r.summary || "")}</div>
       <div class="genesis-actions">
         <button onclick="closeModal('genesis-modal')">先这样</button>
         <button class="primary" onclick="retryFailedSeed()">继续生成</button>
       </div>`,
      "世界生成了，但有些内容没通过检查。"
    );
    return;
  }
  clearSeedForm();
  // 世界包已经带好界面时不再多问一步，直接开始游戏
  const uiMode = (findPack(r.pack_id) || {}).ui_mode || "ask";
  const opening = `<button class="${uiMode === "ask" ? "" : "primary"}" onclick="closeModal('genesis-modal'); startOpeningScene();">${uiMode === "ask" ? "跳过，写出序章第一幕" : "写出序章第一幕"}</button>`;
  const customize =
    uiMode === "ask"
      ? `<button class="primary" onclick="switchGenesisStep(4)">下一步：定制界面</button>`
      : uiMode === "default"
      ? `<button onclick="switchGenesisStep(4)">定制界面</button>`
      : "";
  showGenesisResult(
    `<h4><span>世界「${escapeHtml(r.save_name)}」已生成</span></h4>
     <div style="font-size:0.8rem;color:var(--text-muted);margin-bottom:6px;">${made}</div>
     <div class="genesis-actions">${uiMode === "ask" ? opening + customize : customize + opening}</div>`,
    `世界「${r.save_name}」已生成。`
  );
}

function renderCustomizeOutcome() {
  const themeSel = document.getElementById("theme-selector");
  if (themeSel) themeSel.value = "pack";
  writeMoodLock("");
  renderAll();
  const moodPresets = (currentState && currentState.theme && currentState.theme.mood_presets) || {};
  const moodPreviewBtns = Object.entries(moodPresets)
    .map(([mk, mp]) => `<button type="button" onclick="onChangeWorldMood(${escapeHtml(JSON.stringify(mk))})">${escapeHtml((mp && mp.label) || mk)}</button>`)
    .concat(Object.keys(moodPresets).length ? `<button type="button" onclick="onChangeWorldMood('__auto__')">🔁 恢复自动</button>` : "")
    .join("");
  showGenesisResult(
    `<h4><span>界面已换好</span></h4>
     ${moodPreviewBtns ? `<div class="quick-commands" style="margin-bottom:8px;"><span class="detected-note">试试不同氛围：</span>${moodPreviewBtns}</div>` : ""}
     <div class="genesis-actions">
       <button onclick="closeModal('genesis-modal')">关闭</button>
       <button class="primary" onclick="closeModal('genesis-modal'); startOpeningScene();">写出序章第一幕</button>
     </div>`,
    "界面已换好。"
  );
}

async function runGenesisConceive() {
  const pack_id = document.getElementById("gen-pack-id").value.trim() || `world_${Date.now().toString(36)}`;
  const source_pack_id = document.getElementById("gen-source-pack").value;
  const concept = document.getElementById("gen-concept").value.trim();
  if (!concept && !source_pack_id) return showError("先写几句你想玩的世界");
  await startGenesisJob({ stage: "conceive", pack_id, source_pack_id, concept }, "设计世界失败");
}

async function runGenesisSeed() {
  const pack_id = document.getElementById("gen-seed-pack").value;
  const world_name = document.getElementById("gen-world-name").value.trim();
  const protagonist_pref = document.getElementById("gen-protagonist-pref").value.trim();
  if (!pack_id) return showError("请先在第 ② 步选择一个世界");
  if (!world_name) return showError("给这一局起个名字");
  const nameState = seedNameState(world_name, pack_id);
  if (nameState.kind === "conflict") return showError(nameState.message);
  let setup_answers;
  try {
    setup_answers = collectSetupAnswers("gen", pack_id);
  } catch (e) {
    return showError(e.message);
  }
  await startGenesisJob({ stage: "seed", pack_id, world_name, protagonist_pref, setup_answers }, "生成世界失败");
}

async function runGenesisCustomizeUi() {
  const pack_id = document.getElementById("gen-ui-pack").value;
  const ui_pref = document.getElementById("gen-ui-pref").value.trim();
  if (!pack_id) return showError("请选择要定制的世界");
  await startGenesisJob({ stage: "customize_ui", pack_id, ui_pref }, "定制界面失败");
}

// ---------- 重新生成的世界：旧世界没动过，由玩家决定用哪个 ----------
function pendingWorldCard(p, index) {
  const c = p.counts || {};
  const old = p.old_turns ? `旧世界没有变动（已玩 ${p.old_turns} 回合）。` : "旧世界没有变动。";
  if (p.state === "ready") {
    return `<div class="pending-card">
      <div class="pending-title">「${escapeHtml(p.replaces)}」的新版本生成好了</div>
      <div class="pending-desc">势力 ${c.factions || 0} · 地点 ${c.locations || 0} · 人物 ${c.npcs || 0}。${old}</div>
      <div class="pending-actions">
        <button type="button" class="primary" onclick="resolvePendingWorld(${index}, 'use_new')">用新的</button>
        <button type="button" onclick="resolvePendingWorld(${index}, 'keep_old')">保留旧的</button>
        <button type="button" onclick="resolvePendingWorld(${index}, 'keep_both')">都留下</button>
      </div>
      <div class="pending-note">用新的：旧世界改名留作备份，随时能在存档列表切回去。保留旧的：丢弃这次新生成的内容。都留下：新世界另存为一局。</div>
    </div>`;
  }
  return `<div class="pending-card">
    <div class="pending-title">「${escapeHtml(p.replaces)}」的重新生成没有完成</div>
    <div class="pending-desc">${p.error ? `${escapeHtml(p.error)}。` : ""}${old}</div>
    <div class="pending-actions">
      <button type="button" class="primary" onclick="resumePendingWorld(${index})">继续生成</button>
      <button type="button" onclick="resolvePendingWorld(${index}, 'keep_old')">放弃这次</button>
    </div>
  </div>`;
}

function renderPendingWorlds() {
  const list = (currentState && currentState.pending_worlds) || [];
  const html = currentJob ? "" : list.map(pendingWorldCard).join("");
  ["pending-world-bar", "genesis-pending-box"].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = html;
    el.style.display = html ? "block" : "none";
  });
  renderSeedFormHint();
}

function resumePendingWorld(index) {
  const p = ((currentState && currentState.pending_worlds) || [])[index];
  if (p) resumeSeed(p.replaces, p.pack);
}

async function resolvePendingWorld(index, action) {
  const p = ((currentState && currentState.pending_worlds) || [])[index];
  if (!p) return;
  if (action === "keep_old") {
    const ok = await askConfirm({
      title: p.state === "ready" ? `丢弃「${p.replaces}」的新版本？` : "放弃这次重新生成？",
      body: "这次生成的内容会被删除，不能恢复。旧世界不受影响。",
      yes: "丢弃",
      no: "再想想",
    });
    if (!ok) return;
  }
  let data;
  try {
    const res = await fetch("/api/genesis/pending", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: p.name, action }),
    });
    data = await res.json();
    if (!res.ok) throw new Error(data.detail || "操作失败");
  } catch (e) {
    return showError(describeFetchError(e));
  }
  currentState = data.hud;
  renderAll();
  if (action === "keep_old") return showToast("已保留旧世界，这次新生成的内容已丢弃。", 5000);

  const message =
    action === "use_new"
      ? `现在玩的是新的「${data.save_name}」。${data.backup ? `旧世界保存为「${data.backup}」，可以在左上角的存档列表切回去。` : ""}`
      : `新世界另存为「${data.save_name}」，旧世界还在存档列表里。`;
  const fresh = !(currentState.story_turns || []).length;
  showGenesisResult(
    `<h4><span>${escapeHtml(message)}</span></h4>
     <div class="genesis-actions">
       <button onclick="closeModal('genesis-modal')">关闭</button>
       ${fresh ? `<button class="primary" onclick="closeModal('genesis-modal'); startOpeningScene();">写出序章第一幕</button>` : ""}
     </div>`,
    message
  );
}

// ---------- 第 ③ 步表单：填名字时就说清楚会发生什么 ----------
function seedNameState(name, packId) {
  const saves = (currentState && currentState.saves) || [];
  const existing = saves.find((s) => s.name === name);
  if (!existing) {
    const taken = ((currentState && currentState.pending_worlds) || []).some((p) => p.name === name);
    return taken
      ? { kind: "conflict", message: `「${name}」这个名字正被一次重新生成占用，请换一个名字。` }
      : { kind: "new", message: "", button: "开始生成" };
  }
  if (existing.pack && existing.pack !== packId) {
    const packName = (findPack(existing.pack) || {}).name || existing.pack;
    return { kind: "conflict", message: `「${name}」属于另一个世界（${packName}），不能混在一起。要玩这个世界，请换一个名字。` };
  }
  if (existing.state === "interrupted") {
    return { kind: "resume", message: `「${name}」上次没有生成完，会接着生成，已有的内容不会丢。`, button: "继续生成" };
  }
  if (existing.state === "empty") {
    return { kind: "fill", message: `「${name}」还是空的，世界会直接生成在里面。`, button: "开始生成" };
  }
  const pending = ((currentState && currentState.pending_worlds) || []).find((p) => p.replaces === name);
  if (pending && pending.state === "ready") {
    return { kind: "conflict", message: `「${name}」上次重新生成的结果还没决定，请先在上面选择用哪个。` };
  }
  if (pending) {
    return { kind: "resume", message: `「${name}」上次重新生成没有完成，会接着生成，旧世界不受影响。`, button: "继续生成" };
  }
  return {
    kind: "regenerate",
    message: `「${name}」已经有世界了。这次会另外生成一份新的，旧世界不会被改动，生成完由你决定用哪个。`,
    button: "重新生成一份",
  };
}

function renderSeedFormHint() {
  const packEl = document.getElementById("gen-seed-pack");
  const nameEl = document.getElementById("gen-world-name");
  if (!packEl || !nameEl) return;
  const info = document.getElementById("gen-seed-pack-info");
  const pack = findPack(packEl.value);
  info.textContent = (pack && pack.description) || "";
  info.style.display = info.textContent ? "block" : "none";

  const name = nameEl.value.trim();
  const state = name ? seedNameState(name, packEl.value) : { kind: "new", message: "", button: "开始生成" };
  const hint = document.getElementById("gen-name-hint");
  hint.textContent = state.message;
  hint.style.display = state.message ? "block" : "none";
  hint.classList.toggle("conflict", state.kind === "conflict");

  const running = Boolean(currentJob);
  const btn = document.getElementById("gen-btn-3");
  if (state.button) btn.textContent = state.button;
  btn.disabled = running || state.kind === "conflict";
  document.getElementById("gen-blank-btn").disabled = running || state.kind !== "new";
}

function onSeedPackChange() {
  const packEl = document.getElementById("gen-seed-pack");
  renderSetupFields("gen", packEl.value);
  renderSeedFormHint();
}

async function startOpeningScene() {
  const input = document.getElementById("action-input");
  input.value = "请根据当前世界的开局场景、主角身份与在场人物，正式写出序章第一幕。";
  await submitTurn();
}

// ==================== 世界观包浏览、深度体检与热切换中心 (Pack Hub) ====================

async function openPackBrowserModal() {
  document.getElementById("pack-browser-modal").style.display = "flex";
  await refreshPackBrowser(currentState ? currentState.pack_id : null);
}

async function refreshPackBrowser(preferPackId = null) {
  const res = await fetch("/api/packs");
  const data = await res.json();
  const packs = data.packs || [];
  if (currentState) currentState.packs = packs;

  const listEl = document.getElementById("pack-browser-list");
  if (!packs.length) {
    listEl.innerHTML = `<div style="color:var(--text-muted);font-size:0.82rem;"><code>packs/</code> 目录下暂无世界观文件夹。</div>`;
    document.getElementById("pack-browser-detail").innerHTML = `
      <div class="hud-section">
        <p style="margin-bottom:10px;">当前 <code>packs/</code> 目录为空。你可以直接点击右上角「导入 .rpgpack/.zip」，或打开创世向导拖入/生成世界规则包。</p>
        <button class="primary" onclick="closeModal('pack-browser-modal'); openGenesisModal(2);">✨ 打开世界配置向导</button>
      </div>`;
    return;
  }

  const targetId =
    (preferPackId && packs.some((p) => p.id === preferPackId) && preferPackId) ||
    (currentInspectedPack && packs.some((p) => p.id === currentInspectedPack.id) && currentInspectedPack.id) ||
    packs[0].id;

  listEl.innerHTML = packs
    .map((p) => {
          const problem = p.status === "ready" ? "" : `<span class="pack-list-note">需要处理</span>`;
      return `
        <div class="pack-list-item ${p.id === targetId ? "selected" : ""}" data-pack="${escapeHtml(p.id)}" onclick="inspectPackInBrowser(${escapeHtml(JSON.stringify(p.id))})">
          <div class="pack-list-name">${escapeHtml(p.name)}</div>
          ${problem}
        </div>`;
    })
    .join("");

  await inspectPackInBrowser(targetId);
}

async function inspectPackInBrowser(packId) {
  const res = await fetch(`/api/packs/detail?pack_id=${encodeURIComponent(packId)}`);
  if (!res.ok) return;
  currentInspectedPack = await res.json();
  const docs = currentInspectedPack.docs || {};
  const docKeys = Object.keys(docs);
  currentInspectedDocKey = docKeys.length ? docKeys[0] : "";

  document.querySelectorAll("#pack-browser-list .pack-list-item").forEach((el) => {
    el.classList.toggle("selected", el.dataset.pack === packId);
  });

  renderPackBrowserDetail();
}

function selectPackDocTab(docKey) {
  currentInspectedDocKey = docKey;
  renderPackBrowserDetail();
}

async function flattenNestedPack(packId) {
  const res = await fetch("/api/packs/flatten", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pack_id: packId }),
  });
  const data = await res.json();
  if (!res.ok) return showError("扁平化修复失败：" + (data.detail || ""));
  if (data.hud) {
    currentState = data.hud;
    renderAll();
  }
  await refreshPackBrowser(packId);
}

const PACK_DOC_NAMES = { "setting.md": "世界设定", "rules.md": "规则", "protagonist.md": "主角", "style_pack.md": "文风" };

function renderPackBrowserDetail() {
  const box = document.getElementById("pack-browser-detail");
  const p = currentInspectedPack;
  if (!box || !p) return;
  const docs = p.docs || {};
  const readable = Object.keys(docs).filter((k) => PACK_DOC_NAMES[k]);
  const technical = Object.keys(docs).filter((k) => !PACK_DOC_NAMES[k]);
  const tab = (k, label) => `<button class="doc-tab ${k === currentInspectedDocKey ? "active" : ""}" onclick="selectPackDocTab(${escapeHtml(JSON.stringify(k))})">${escapeHtml(label)}</button>`;
  const current = docs[currentInspectedDocKey] || "";
  const viewer = PACK_DOC_NAMES[currentInspectedDocKey]
    ? `<div class="doc-viewer md-view md-rich">${renderMdLite(current)}</div>`
    : `<div class="doc-viewer">${escapeHtml(current)}</div>`;

  let action;
  if (p.status === "nested") {
    action = `<button class="primary" onclick="flattenNestedPack(${escapeHtml(JSON.stringify(p.id))})">修复文件夹结构</button>`;
  } else if (p.status === "unparsed") {
    action = `<button class="primary" onclick="closeModal('pack-browser-modal'); openGenesisWithUnparsed(${escapeHtml(JSON.stringify(p.id))});">让 AI 整理成世界包</button>`;
  } else {
    action = `<button class="primary" onclick="closeModal('pack-browser-modal'); openNewGame(${escapeHtml(JSON.stringify(p.id))});">用这个世界开始新游戏</button>`;
  }
  const errors = (p.errors || []).map((e) => `<li>${escapeHtml(e)}</li>`).join("");

  box.innerHTML = `
    <div class="pack-detail-head">
      <div>
        <div class="pack-detail-name">${escapeHtml(p.name)}</div>
        <div class="pack-detail-genre">${escapeHtml(p.genre || "")}</div>
      </div>
      ${action}
    </div>
    <p class="pack-detail-desc">${escapeHtml(p.description || "")}</p>
    ${errors ? `<ul class="pack-detail-errors">${errors}</ul>` : ""}
    <div class="doc-tabs">
      ${readable.map((k) => tab(k, PACK_DOC_NAMES[k])).join("")}
      ${technical.map((k) => tab(k, k)).join("")}
    </div>
    ${viewer}`;
}

function openGenesisWithUnparsed(packId) {
  openGenesisModal(2);
  const srcSel = document.getElementById("gen-source-pack");
  if (srcSel) srcSel.value = packId;
  document.getElementById("gen-pack-id").value = packId;
}

// ==================== 像素天际线与故事流 ====================

function drawSkyline(px) {
  const canvas = document.getElementById("skyline-canvas");
  if (!canvas || !px.length) return;
  const H = px.length;
  const W = px[0].length;
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext("2d");
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const [r, g, b] = px[y][x];
      ctx.fillStyle = `rgb(${r},${g},${b})`;
      ctx.fillRect(x, y, 1, 1);
    }
  }
}

function formatProseHtml(text, showCaret = false) {
  const escaped = escapeHtml(text || "");
  const paras = escaped.split(/\n\s*\n/).filter((p) => p.trim());
  if (!paras.length) {
    return showCaret ? `<p><span class="typewriter-caret">▍</span></p>` : "";
  }
  return paras
    .map((p, idx) => {
      const trimmed = p.trim();
      const caretHtml = showCaret && idx === paras.length - 1 ? '<span class="typewriter-caret">▍</span>' : "";
      if (trimmed.startsWith("【难关】")) {
        return `<div class="challenge-badge">${trimmed}${caretHtml}</div>`;
      }
      const withDialogue = trimmed.replace(
        /(“[^”]+”|&quot;[^&]+&quot;)/g,
        '<span class="dialogue-span">$1</span>'
      );
      return `<p>${withDialogue.replace(/\n/g, "<br/>")}${caretHtml}</p>`;
    })
    .join("");
}

function renderCommandReplies() {
  return commandReplies
    .map(
      (r) => `
      <article class="turn-card command-reply">
        <div class="turn-meta"><span>命令回复（不计入剧情）</span></div>
        <div class="player-action-box"><strong>你</strong><span>${escapeHtml(r.input)}</span></div>
        <div class="md-view md-rich">${renderMdLite(r.story)}</div>
      </article>`
    )
    .join("");
}

function combinedStoryTurns() {
  const latest = currentState.story_turns || [];
  const firstIndex = latest.length ? latest[0].index : Infinity;
  return olderTurns.filter((t) => t.index < firstIndex).concat(latest);
}

function scrollCardToTop(stream, card) {
  if (!stream || !card) return;
  stream.scrollTop += card.getBoundingClientRect().top - stream.getBoundingClientRect().top - 8;
}

// 记住“正在看哪一轮、看到它的哪里”，而不是记像素位置：主题、字体加载后页面高度会变，像素位置会偏
function storyAnchor(stream) {
  const top = stream.getBoundingClientRect().top;
  for (const card of stream.querySelectorAll("article.turn-card[data-turn]")) {
    const r = card.getBoundingClientRect();
    if (r.bottom > top) return { turn: card.dataset.turn, offset: r.top - top };
  }
  return null;
}

function restoreStoryAnchor(stream, anchor) {
  const card = anchor && stream.querySelector(`article.turn-card[data-turn="${anchor.turn}"]`);
  if (!card) return false;
  stream.scrollTop += card.getBoundingClientRect().top - stream.getBoundingClientRect().top - anchor.offset;
  return true;
}

function scrollToLatestTurnStart(stream) {
  const go = () => {
    const cards = stream.querySelectorAll("article.turn-card[data-turn]");
    scrollCardToTop(stream, cards[cards.length - 1]);
  };
  go();
  // 打开页面时样式和字体还没加载完，页面会继续变高：玩家没自己动过滚动条，就在加载完后再对齐一次
  let moved = false;
  const mark = () => {
    moved = true;
  };
  ["wheel", "touchstart", "mousedown", "keydown"].forEach((ev) => stream.addEventListener(ev, mark, { once: true, passive: true }));
  const settle = () => {
    if (!moved) go();
  };
  requestAnimationFrame(settle);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(settle);
  setTimeout(settle, 400);
  setTimeout(settle, 1500);
}

async function loadOlderTurns() {
  const turns = combinedStoryTurns();
  if (!turns.length || !currentState) return;
  const btn = document.getElementById("older-turns-btn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "正在加载…";
  }
  try {
    const qs = new URLSearchParams({ before: String(turns[0].index), limit: "20", save_name: currentState.save_name || "" });
    const res = await fetch(`/api/story?${qs}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "加载失败");
    const stream = document.getElementById("story-stream");
    // 往上插入内容后，让正在看的那一段留在原处
    storyScrollIntent = { anchor: storyAnchor(stream) };
    olderTurns = (data.turns || []).concat(olderTurns);
    renderStoryStream();
  } catch (e) {
    showError(describeFetchError(e));
    if (btn) {
      btn.disabled = false;
      btn.textContent = "往前翻";
    }
  }
}

function renderStoryStream() {
  const stream = document.getElementById("story-stream");
  const turns = combinedStoryTurns();
  // 翻出来的旧回合和这一页接上：新一轮进来后最新一页会往后挪，挪出去的那几轮留在这里，不会从页面上消失
  if (olderTurns.length) olderTurns = turns.slice();
  const previousAnchor = storyAnchor(stream);
  const previousTop = stream.scrollTop;
  if (!turns.length) {
    const ec = currentState.entity_counts || {};
    const seedState = currentState.seed_state || "";
    const hasSeededEntities = !seedState && (ec.npcs || 0) + (ec.locations || 0) > 0;
    stream.innerHTML = `
      <div class="turn-card">
        <div class="turn-meta"><span>${escapeHtml(currentState.world_title || "")}</span></div>
        <div class="story-prose">
          <p>${
            seedState === "running"
              ? "世界正在生成，完成后就可以开始。"
              : seedState === "interrupted"
              ? `世界上次没有生成完${currentState.seed_error ? `：${escapeHtml(currentState.seed_error)}` : "。"}可以接着生成，已有的内容不会丢。`
              : hasSeededEntities
              ? "世界准备好了。写出序章第一幕，或者直接在下面输入你的第一个行动。"
              : "这一局还是空的。打开开局向导生成世界，就可以开始了。"
          }</p>
        </div>
        <div class="turn-toolbar">
          ${seedState === "interrupted" ? `<button class="primary" onclick="retryInterruptedSeed()">继续生成</button>` : seedState === "running" ? "" : `<button onclick="openGenesisModal()">打开开局向导</button>`}
          ${hasSeededEntities ? `<button class="primary" onclick="startOpeningScene()">写出序章第一幕</button>` : ""}
        </div>
      </div>${renderCommandReplies()}`;
    syncCardCornerOrnaments();
    requestAnimationFrame(renderDialoguePixelFrame);
    return;
  }

  const earlier = turns[0].index - 1;
  const olderBar = earlier > 0
    ? `<div class="older-turns-bar"><button type="button" id="older-turns-btn" onclick="loadOlderTurns()">往前翻（更早还有 ${earlier} 轮）</button></div>`
    : "";
  stream.innerHTML = olderBar + turns
    .map((t, idx) => {
      const isLast = idx === turns.length - 1;
      const actHtml = t.input
        ? `<div class="player-action-box"><strong>你</strong><span>${escapeHtml(t.input)}</span></div>`
        : "";
      const toolbarHtml = isLast
        ? `<div class="turn-toolbar">
             <button onclick="promptEditLastStory()">润色正文</button>
             <button onclick="swipeLastTurn()">重演 / 撤回</button>
           </div>`
        : "";
      return `
        <article class="turn-card" data-idx="${idx}" data-turn="${t.index || idx + 1}">
          <div class="turn-meta">
            <span>${escapeHtml(t.volume)} · ${escapeHtml(t.date_header)}</span>
            <span>#${t.index || idx + 1}</span>
          </div>
          ${actHtml}
          <div class="story-prose">${formatProseHtml(t.story)}</div>
          ${toolbarHtml}
        </article>`;
    })
    .join("") + renderCommandReplies();
  syncCardCornerOrnaments();
  if (storyScrollIntent === "latest") {
    // 打开或切换存档时，从最新一轮的开头读起
    scrollToLatestTurnStart(stream);
  } else if (storyScrollIntent && typeof storyScrollIntent === "object") {
    if (!restoreStoryAnchor(stream, storyScrollIntent.anchor)) stream.scrollTop = previousTop;
  } else if (!restoreStoryAnchor(stream, previousAnchor)) {
    stream.scrollTop = previousTop;
  }
  storyScrollIntent = "keep";
  requestAnimationFrame(renderDialoguePixelFrame);
}

function fillCommand(cmd) {
  const input = document.getElementById("action-input");
  input.value = cmd;
  input.focus();
}

async function runStreamingTurnFlow(url, payload, actionPreviewText, isSwipe = false) {
  const stream = document.getElementById("story-stream");
  const btn = document.getElementById("send-btn");
  const inputEl = document.getElementById("action-input");
  if (activeTurnAbortCtrl) return; // 防止重复提交
  activeTurnAbortCtrl = new AbortController();
  // 发出去就清空，避免看着像还没发；这一轮没生效时再放回去
  const sentText = inputEl ? inputEl.value : "";
  if (!isSwipe && inputEl) inputEl.value = "";
  const restoreInput = () => {
    if (!isSwipe && inputEl && !inputEl.value.trim()) inputEl.value = sentText;
  };
  const saveSelector = document.getElementById("save-selector");
  if (saveSelector) saveSelector.disabled = true;
  const turnStartedAt = Date.now();
  const refreshStopBtn = () => {
    if (!btn) return;
    const sec = Math.max(0, Math.floor((Date.now() - turnStartedAt) / 1000));
    btn.textContent = `⏹ 停止生成 (${sec}s)`;
  };
  if (btn) {
    btn.disabled = false;
    btn.classList.add("danger-stop");
    btn.classList.remove("primary");
    refreshStopBtn();
  }
  const turnElapsedTimer = setInterval(refreshStopBtn, 1000);

  // Swipe 重演时先在界面上折叠上一轮旧卡片，插入实时流式草稿卡
  if (isSwipe && stream) {
    const cards = stream.querySelectorAll("article.turn-card");
    if (cards.length) {
      cards[cards.length - 1].style.opacity = "0.35";
    }
  }

  let draftCard = null;
  let placeholderEl = null;
  let placeholderTextEl = null;
  let proseEl = null;

  if (stream) {
    const cleanAct = stripInlineOocPreview(actionPreviewText);
    draftCard = document.createElement("article");
    draftCard.className = "turn-card streaming-card";
    draftCard.id = "streaming-turn-card";
    // 和写完后的那一轮用同一个编号，换成正式卡片时视线不跳
    const shown = combinedStoryTurns();
    if (!isSwipe) draftCard.dataset.turn = String(shown.length ? shown[shown.length - 1].index + 1 : 1);
    draftCard.innerHTML = `
      <div class="turn-meta">
        <span>${isSwipe ? "↩️ 时间线重演中" : "✨ 当前回合"}</span>
        <span>LIVE</span>
      </div>
      ${cleanAct ? `<div class="player-action-box"><strong>你的行动：</strong> ${escapeHtml(cleanAct)}</div>` : ""}
      <div class="streaming-placeholder" id="streaming-placeholder">
        <span class="live-pulse-dot"></span>
        <span id="streaming-card-phase">${isSwipe ? "↩️ 正在回溯上一轮时间线快照…" : "正在感知当前场景脉络…"}</span>
      </div>
      <div class="story-prose" id="streaming-prose" style="display:none;"></div>`;
    stream.appendChild(draftCard);
    placeholderEl = draftCard.querySelector("#streaming-placeholder");
    placeholderTextEl = draftCard.querySelector("#streaming-card-phase");
    proseEl = draftCard.querySelector("#streaming-prose");
    // 只在开始时把新一轮的开头放到眼前，之后不自动往下滚，玩家按自己的速度读
    scrollCardToTop(stream, draftCard);
  }

  setLiveStatusBar(isSwipe ? "↩️ 正在回溯上一轮时间线快照…" : "正在感知当前场景脉络…", false);

  const typewriter = createSmoothTypewriter((currentTxt, showCaret) => {
    if (!proseEl) return;
    if (currentTxt) {
      if (placeholderEl) placeholderEl.style.display = "none";
      proseEl.style.display = "block";
      proseEl.innerHTML = formatProseHtml(currentTxt, showCaret);
    } else {
      proseEl.style.display = "none";
      proseEl.innerHTML = "";
      if (placeholderEl) placeholderEl.style.display = "flex";
    }
  });

  try {
    const data = await consumeSsePost(url, payload, {
      onStatus(ev) {
        const label = ev.label || "正在构思叙事脉络…";
        const isWorldSim = Boolean(ev.world_sim);
        setLiveStatusBar(label, isWorldSim);
        if (draftCard) draftCard.classList.toggle("world-sim-mode", isWorldSim);
        if (placeholderEl) placeholderEl.classList.toggle("world-sim-mode", isWorldSim);
        if (placeholderTextEl && !typewriter.hasText()) {
          placeholderTextEl.textContent = label;
        }
      },
      onDelta(chunk) {
        typewriter.push(chunk);
      },
      onStepReset() {
        typewriter.reset();
      },
    }, activeTurnAbortCtrl.signal);

    typewriter.finish();
    if (data) {
      commandReplies = data.command && !data.archived && data.story ? [...commandReplies, { input: actionPreviewText, story: data.story }] : [];
      if (data.hud) {
        currentState = data.hud;
        renderAll();
      }
      if (data.message) {
        showToast(data.message, 5000);
      }
    }
  } catch (e) {
    if (draftCard && draftCard.parentElement) {
      draftCard.remove();
    }
    restoreInput();
    if (e.name === "AbortError") {
      // 后端在下一次回调处抛出 StreamCancelled 并执行回合级原子回滚；稍候刷新以对齐真实存档
      await new Promise((r) => setTimeout(r, 600));
      await fetchState();
      showToast(isSwipe ? "⏹ 已停止重演，上一轮已原样恢复" : "⏹ 已停止本轮生成，存档已回退到本轮之前，你的输入已放回输入框", 5200);
    } else {
      await fetchState();
      showError(isSwipe ? e.message : `本轮没有生效，存档已恢复到行动之前（你的输入已放回输入框）：\n${describeFetchError(e)}`);
    }
  } finally {
    clearInterval(turnElapsedTimer);
    activeTurnAbortCtrl = null;
    if (saveSelector) saveSelector.disabled = false;
    setLiveStatusBar("", false);
    if (btn) {
      btn.disabled = false;
      btn.classList.remove("danger-stop");
      btn.classList.add("primary");
      btn.textContent = "发送行动";
    }
  }
}

function requestBackendStreamCancel() {
  return fetch("/api/stream/cancel", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  }).catch(() => {});
}

function handlePrimarySendBtn() {
  if (activeTurnAbortCtrl) {
    const btn = document.getElementById("send-btn");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "正在停止…";
    }
    requestBackendStreamCancel().finally(() => {
      if (activeTurnAbortCtrl) activeTurnAbortCtrl.abort();
    });
    return;
  }
  submitTurn();
}

async function submitTurn() {
  if (activeTurnAbortCtrl) return;
  const inputEl = document.getElementById("action-input");
  const text = inputEl.value.trim();
  if (!text) return;
  await runStreamingTurnFlow("/api/turn/stream", { input: text }, text, false);
}

function toggleInlineSwipeBar(show) {
  const bar = document.getElementById("inline-swipe-bar");
  if (!bar) return;
  bar.style.display = show ? "flex" : "none";
  const inp = document.getElementById("swipe-feedback-input");
  if (show && inp) {
    inp.value = "";
    inp.focus();
  }
}

function swipeLastTurn() {
  if (activeTurnAbortCtrl) return;
  const turns = (currentState && currentState.story_turns) || [];
  if (!turns.length) {
    showToast("当前还没有可重演的回合");
    return;
  }
  toggleInlineSwipeBar(true);
}

async function confirmInlineSwipe() {
  const inp = document.getElementById("swipe-feedback-input");
  const feedback = inp ? inp.value.trim() : "";
  toggleInlineSwipeBar(false);
  const turns = (currentState && currentState.story_turns) || [];
  const lastInput = turns.length ? turns[turns.length - 1].input || "" : "";
  await runStreamingTurnFlow("/api/turn/swipe/stream", { feedback }, lastInput, true);
}

async function confirmInlineUndo() {
  if (activeTurnAbortCtrl) return;
  toggleInlineSwipeBar(false);
  try {
    const res = await fetch("/api/turn/undo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "撤回失败");
    currentState = data.hud;
    renderAll();
    const inputEl = document.getElementById("action-input");
    if (inputEl && data.last_input && !inputEl.value.trim()) inputEl.value = data.last_input;
    showToast(data.message || "已撤回上一轮", 5200);
  } catch (e) {
    showError(e.message);
  }
}

function promptEditLastStory() {
  const turns = (currentState && currentState.story_turns) || [];
  if (!turns.length) return;
  const last = turns[turns.length - 1];
  const cards = document.querySelectorAll("#story-stream article.turn-card");
  const card = cards.length ? cards[cards.length - 1] : null;
  if (!card || card.querySelector(".inline-story-editor-box")) return;
  const prose = card.querySelector(".story-prose");
  const toolbar = card.querySelector(".turn-toolbar");
  if (prose) prose.style.display = "none";
  if (toolbar) toolbar.style.display = "none";

  const box = document.createElement("div");
  box.className = "inline-story-editor-box";
  box.innerHTML = `
    <textarea class="inline-story-editor"></textarea>
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
      <span style="font-size:0.74rem;color:var(--text-muted);">仅改写本轮正文措辞，不回滚世界状态（Ctrl+Enter 保存 / Esc 取消）</span>
      <div style="display:flex;gap:8px;">
        <button type="button" class="inline-edit-cancel">取消</button>
        <button type="button" class="primary inline-edit-save">💾 保存修改</button>
      </div>
    </div>`;
  card.appendChild(box);
  const ta = box.querySelector("textarea");
  ta.value = last.story || "";
  ta.focus();

  const closeEditor = () => {
    box.remove();
    if (prose) prose.style.display = "";
    if (toolbar) toolbar.style.display = "";
  };
  const saveEditor = async () => {
    const edited = ta.value;
    if (!edited.trim() || edited.trim() === String(last.story || "").trim()) {
      closeEditor();
      return;
    }
    const saveBtn = box.querySelector(".inline-edit-save");
    saveBtn.disabled = true;
    saveBtn.textContent = "保存中…";
    try {
      const res = await fetch("/api/story/edit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ new_story: edited }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "保存失败");
      if (data.hud) {
        currentState = data.hud;
        renderAll();
      } else {
        closeEditor();
      }
      showToast("✓ 已原地润色本轮正文");
    } catch (e) {
      saveBtn.disabled = false;
      saveBtn.textContent = "💾 保存修改";
      showToast("✗ 润色保存失败：" + e.message, 5000);
    }
  };
  box.querySelector(".inline-edit-cancel").onclick = closeEditor;
  box.querySelector(".inline-edit-save").onclick = saveEditor;
  ta.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") {
      ev.preventDefault();
      ev.stopPropagation();
      saveEditor();
    } else if (ev.key === "Escape") {
      ev.preventDefault();
      closeEditor();
    }
  });
}

// 划词正反例调教台（内联原因输入，免阻塞弹窗）
document.addEventListener("mouseup", (e) => {
  const pop = document.getElementById("selection-popover");
  if (e.target.closest("#selection-popover")) return;
  const sel = window.getSelection();
  const text = sel ? sel.toString().trim() : "";
  if (text.length >= 12 && e.target.closest(".story-prose")) {
    selectedExcerpt = text;
    const reasonEl = document.getElementById("selection-reason-input");
    if (reasonEl) reasonEl.value = "";
    pop.style.display = "flex";
    pop.style.left = `${Math.max(8, Math.min(window.innerWidth - 470, e.clientX))}px`;
    pop.style.top = `${Math.max(12, e.clientY - 48)}px`;
  } else {
    pop.style.display = "none";
  }
});

async function submitStyleExample(liked) {
  const pop = document.getElementById("selection-popover");
  const reasonEl = document.getElementById("selection-reason-input");
  const reason = reasonEl ? reasonEl.value.trim() : "";
  pop.style.display = "none";
  if (!selectedExcerpt) return;

  try {
    const res = await fetch("/api/style/example", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ excerpt: selectedExcerpt, liked, reason }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "记录失败");
    showToast(`✓ 已收入「${data.file}」的 ${data.target}，并同步记入 playtest.md`);
  } catch (e) {
    showToast("✗ 范例记录失败：" + e.message, 5000);
  }
  selectedExcerpt = "";
}

// ==================== 配置、新建存档与导出模态框 ====================

function toggleConfigModalDriverFields() {
  const dtype = document.getElementById("cfg-front-type").value || "api";
  document.getElementById("cfg-pane-api").style.display = dtype === "api" ? "block" : "none";
  document.getElementById("cfg-pane-cli").style.display = dtype === "cli" ? "block" : "none";
  document.getElementById("cfg-pane-harness").style.display = dtype === "harness" ? "block" : "none";
  syncBackModelCard("cfg", true);
}

function openConfigModal() {
  const cfg = (currentState && currentState.config) || {};
  const fd = cfg.front_driver || {};
  const np = cfg.narrative_prompt || {};
  document.getElementById("cfg-front-type").value = fd.type || "api";
  document.getElementById("cfg-api-format").value = fd.api_format || "openai";
  document.getElementById("cfg-api-base").value = fd.api_base || "https://api.deepseek.com/v1";
  const keyInput = document.getElementById("cfg-api-key");
  keyInput.value = "";
  keyInput.placeholder = fd.has_saved_key ? "已保存密钥（留空保持不变，输入新值可覆盖）" : "sk-...";
  document.getElementById("cfg-model").value = fd.model || "deepseek-chat";
  document.getElementById("cfg-cli-tool").value = fd.cli_tool || "claude";
  document.getElementById("cfg-front-effort").value = frontEffortOf(fd);
  document.getElementById("cfg-back-effort").value = (cfg.back_driver || {}).effort || "high";
  document.getElementById("cfg-harness-mode").value = fd.harness_mode || "external_ide";
  document.getElementById("cfg-narrative-prompt").value = np.authors_note_template || DEFAULT_UNIVERSAL_PROMPT;
  document.getElementById("cfg-verify-feedback").textContent = "";
  renderBackModelCard("cfg");
  toggleConfigModalDriverFields();
  document.getElementById("config-modal").style.display = "flex";
}

function resetDefaultPrompt() {
  document.getElementById("cfg-narrative-prompt").value = DEFAULT_UNIVERSAL_PROMPT;
}

function _collectConfigModalDriverPayload() {
  return {
    type: document.getElementById("cfg-front-type").value,
    api_format: document.getElementById("cfg-api-format").value,
    api_base: document.getElementById("cfg-api-base").value.trim(),
    api_key: document.getElementById("cfg-api-key").value.trim(),
    model: document.getElementById("cfg-model").value.trim(),
    cli_tool: document.getElementById("cfg-cli-tool").value,
    effort: document.getElementById("cfg-front-effort").value,
    harness_mode: document.getElementById("cfg-harness-mode").value,
  };
}

async function testConfigModalDriver() {
  const fb = document.getElementById("cfg-verify-feedback");
  fb.style.color = "var(--accent-primary)";
  fb.textContent = "⏳ 正在实测驱动连接…";
  try {
    const res = await fetch("/api/driver/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        front_driver: _collectConfigModalDriverPayload(),
        back_driver: Object.assign(collectBackDriver("cfg"), { effort: document.getElementById("cfg-back-effort").value }),
        narrative_prompt: {
          authors_note_template: document.getElementById("cfg-narrative-prompt").value.trim() || DEFAULT_UNIVERSAL_PROMPT,
        },
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "验证未通过");
    if (data.back_check && !data.back_check.ok) {
      fb.style.color = "var(--accent-warn)";
      fb.textContent = `台前已连通，幕后模型未通过：${data.back_check.detail}`;
    } else {
      fb.style.color = "var(--accent-ok, #4fb987)";
      fb.textContent = `✓ ${(data.check && data.check.detail) || "验证成功并已保存"}`;
    }
    if (data.hud) {
      currentState = data.hud;
      renderAll();
    }
  } catch (e) {
    fb.style.color = "var(--accent-danger)";
    fb.textContent = `✗ ${e.message}`;
  }
}

async function saveConfig() {
  let backDriver;
  try {
    backDriver = collectBackDriver("cfg");
  } catch (e) {
    const fb = document.getElementById("cfg-verify-feedback");
    fb.style.color = "var(--accent-danger)";
    fb.textContent = `✗ ${e.message}`;
    return;
  }
  const cfg = {
    front_driver: _collectConfigModalDriverPayload(),
    back_driver: { ...backDriver, effort: document.getElementById("cfg-back-effort").value },
    narrative_prompt: {
      authors_note_template: document.getElementById("cfg-narrative-prompt").value.trim() || DEFAULT_UNIVERSAL_PROMPT,
    },
  };
  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cfg),
  });
  const data = await res.json();
  closeModal("config-modal");
  if (data.hud) {
    currentState = data.hud;
    renderAll();
    const gModal = document.getElementById("genesis-modal");
    if (gModal && gModal.style.display === "flex") {
      openGenesisModal(currentGenesisStep);
    }
  } else {
    await fetchState();
  }
}

// 新游戏只有一个入口：向导第 ③ 步。选世界、起名字、开局设置都在这里，不再另开一个窗口
function openNewGame(preferPackId = null) {
  const packs = (currentState && currentState.packs) || [];
  if (!packs.length) return openGenesisModal(1);
  const preferred = preferPackId ? findPack(preferPackId) : null;
  if (preferred && preferred.status !== "ready") return openGenesisWithUnparsed(preferPackId);
  if (!packs.some((p) => p.status === "ready")) return openGenesisModal(2);

  openGenesisModal(3);
  if (currentJob) return; // 有任务在跑时向导只显示进度
  const packEl = document.getElementById("gen-seed-pack");
  if (preferred) packEl.value = preferPackId;
  const nameEl = document.getElementById("gen-world-name");
  nameEl.value = "";
  document.getElementById("gen-protagonist-pref").value = "";
  onSeedPackChange();
  nameEl.focus();
}

async function createBlankSave() {
  const name = document.getElementById("gen-world-name").value.trim();
  const pack_id = document.getElementById("gen-seed-pack").value;
  if (!pack_id) return showError("请先在第 ② 步选择一个世界");
  if (!name) return showError("给这一局起个名字");
  let setup_answers;
  try {
    setup_answers = collectSetupAnswers("gen", pack_id);
  } catch (e) {
    return showError(e.message);
  }
  const res = await fetch("/api/saves/new", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, pack_id, setup_answers }),
  });
  const data = await res.json();
  if (!res.ok) return showError(data.detail || "创建失败");
  closeModal("genesis-modal");
  currentState = data.hud;
  clearSeedForm();
  renderAll();
}

function openExportModal() {
  document.getElementById("export-result").textContent = "";
  document.getElementById("export-modal").style.display = "flex";
}

async function runExport(mode) {
  const box = document.getElementById("export-result");
  box.textContent = "正在打包导出…";
  const res = await fetch("/api/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  const data = await res.json();
  box.textContent = res.ok ? `✓ ${data.message}（路径：${data.path}）` : `✗ 导出失败：${data.detail}`;
}

function closeModal(id) {
  document.getElementById(id).style.display = "none";
}

// 主输入框和调教框：Enter 发送，Shift+Enter 换行。输入法选词时按的回车不算发送
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.shiftKey || e.isComposing || e.keyCode === 229) return;
  const el = document.activeElement;
  const target = el && { "action-input": ["send-btn", submitTurn], "tuner-input": ["tuner-send-btn", submitTunerChat] }[el.id];
  if (!target) return;
  e.preventDefault();
  // 按钮禁用时（有任务在跑）不发送，打好的字留在框里
  if (el.value.trim() && !document.getElementById(target[0]).disabled) target[1]();
});

// 笔记本/高缩放屏（≤1260px 有效宽度）启动时默认收起右侧调教窗，避免浮层遮挡角色与日志面板
function applyResponsiveDockDefault() {
  // 调教窗默认收起，给故事和状态面板留出空间；展开过就记住
  let open = false;
  try {
    open = window.localStorage.getItem("rpg.tunerDockOpen") === "1";
  } catch (_) {}
  if (window.innerWidth <= 1260) open = false; // 窄屏上它会盖住状态面板
  document.getElementById("main-workspace").classList.toggle("tuner-collapsed", !open);
  document.getElementById("tuner-dock-toggle").classList.toggle("active-dock-btn", open);
}

window.addEventListener("DOMContentLoaded", () => {
  applyResponsiveDockDefault();
  fetchState();
});
