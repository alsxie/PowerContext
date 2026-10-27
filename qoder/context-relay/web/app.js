const DEMO_MEMORIES = [
  {
    entry_id: "mem_progress_01",
    text: "项目「小样本异常检测研究」进展：完成基线复现实验，确认验证集上的 F1 为 0.71。",
    score: 0.96,
    matched_by: ["fts"],
  },
  {
    entry_id: "mem_decision_02",
    text: "项目「小样本异常检测研究」决策：采用按设备分组切分，避免同一设备数据泄漏到验证集。",
    score: 0.88,
    matched_by: ["fts"],
  },
  {
    entry_id: "mem_blocker_03",
    text: "项目「小样本异常检测研究」阻塞：设备 C 的时间戳存在 8 小时偏移，需先统一时区。",
    score: 0.84,
    matched_by: ["fts"],
  },
];

const modePill = document.querySelector("#mode-pill");
const modeLabel = document.querySelector("#mode-label");
const checkpointForm = document.querySelector("#checkpoint-form");
const resumeForm = document.querySelector("#resume-form");
const saveButton = document.querySelector("#save-button");
const resumeButton = document.querySelector("#resume-button");
const saveStatus = document.querySelector("#save-status");
const evidenceList = document.querySelector("#evidence-list");
const evidenceTemplate = document.querySelector("#evidence-template");
const contextSummary = document.querySelector("#context-summary");
const hitCount = document.querySelector("#hit-count");
const contextSize = document.querySelector("#context-size");

let liveMode = false;
let demoMemories = loadDemoMemories();

function loadDemoMemories() {
  try {
    const stored = JSON.parse(localStorage.getItem("context-relay-memories") || "[]");
    return Array.isArray(stored) && stored.length ? stored : [...DEMO_MEMORIES];
  } catch {
    return [...DEMO_MEMORIES];
  }
}

function setMode(mode) {
  liveMode = mode === "live";
  modePill.dataset.mode = mode;
  modeLabel.textContent = liveMode ? "PowerContext 已连接" : "浏览器演示模式";
}

async function detectMode() {
  const shouldProbeApi = location.port === "8088" || new URLSearchParams(location.search).has("api");
  if (!shouldProbeApi) {
    setMode("demo");
    return;
  }

  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 1800);
  try {
    const response = await fetch("/api/health", { signal: controller.signal });
    if (!response.ok) throw new Error("backend unavailable");
    setMode("live");
  } catch {
    setMode("demo");
  } finally {
    window.clearTimeout(timeout);
  }
}

async function requestJson(path, options) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `请求失败（${response.status}）`);
  }
  return response.json();
}

function formPayload() {
  const values = Object.fromEntries(new FormData(checkpointForm).entries());
  return Object.fromEntries(Object.entries(values).map(([key, value]) => [key, String(value).trim()]));
}

function checkpointToMemories(payload) {
  const prefix = `项目「${payload.project}」`;
  const values = [
    ["progress", payload.progress],
    ["decision", payload.decision],
    ["blocker", payload.blocker],
    ["next_step", payload.next_step],
  ];
  const labels = { progress: "进展", decision: "决策", blocker: "阻塞", next_step: "下一步" };
  return values
    .filter(([, value]) => value)
    .map(([kind, value], index) => ({
      entry_id: `demo_${kind}_${Date.now()}_${index}`,
      text: `${prefix}${labels[kind]}：${value}`,
      score: 1,
      matched_by: ["local-demo"],
    }));
}

checkpointForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  saveButton.disabled = true;
  saveStatus.dataset.state = "";
  saveStatus.textContent = liveMode ? "正在写入 PowerContext…" : "正在保存到本机演示数据…";
  const payload = formPayload();

  try {
    if (liveMode) {
      const result = await requestJson("/api/checkpoints", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      saveStatus.textContent = `已写入 ${result.saved.length} 条 PowerContext Memory，可在下一次会话检索。`;
    } else {
      const memories = checkpointToMemories(payload);
      demoMemories = [...memories, ...demoMemories].slice(0, 30);
      localStorage.setItem("context-relay-memories", JSON.stringify(demoMemories));
      saveStatus.textContent = `已保存 ${memories.length} 条浏览器演示数据；启动本地服务后可验证真实写入。`;
    }
    saveStatus.dataset.state = "success";
  } catch (error) {
    saveStatus.textContent = error.message;
    saveStatus.dataset.state = "error";
  } finally {
    saveButton.disabled = false;
  }
});

function tokenize(query) {
  const latin = query.toLowerCase().match(/[a-z0-9_.-]+/g) || [];
  const chinese = (query.match(/[\u4e00-\u9fff]+/g) || []).flatMap((phrase) => {
    if (phrase.length <= 2) return [phrase];
    const pairs = Array.from({ length: phrase.length - 1 }, (_, index) => phrase.slice(index, index + 2));
    return [phrase, ...pairs];
  });
  return [...new Set([...latin, ...chinese])];
}

function searchDemo(query) {
  const tokens = tokenize(query);
  return demoMemories
    .map((memory) => {
      const text = memory.text.toLowerCase();
      const matches = tokens.filter((token) => text.includes(token)).length;
      return { ...memory, score: tokens.length ? matches / tokens.length : 0 };
    })
    .filter((memory) => memory.score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, 8);
}

function summarize(hits) {
  if (!hits.length) {
    return "当前没有找到相关工作记忆。换一个更具体的项目名、决定或阻塞关键词再试。";
  }
  const next = hits.find((hit) => hit.text.includes("下一步"));
  const blocker = hits.find((hit) => hit.text.includes("阻塞"));
  const decision = hits.find((hit) => hit.text.includes("决策"));
  const sections = [];
  if (next) sections.push(`<p><strong>现在从这里开始：</strong>${escapeHtml(next.text.split("下一步：")[1] || next.text)}</p>`);
  if (blocker) sections.push(`<p><strong>先处理这个阻塞：</strong>${escapeHtml(blocker.text.split("阻塞：")[1] || blocker.text)}</p>`);
  if (decision) sections.push(`<p><strong>保持已确认决策：</strong>${escapeHtml(decision.text.split("决策：")[1] || decision.text)}</p>`);
  if (!sections.length) sections.push(`<p><strong>相关历史：</strong>${escapeHtml(hits[0].text)}</p>`);
  return sections.join("");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderEvidence(result) {
  const hits = result.hits || [];
  evidenceList.replaceChildren();
  hits.forEach((hit) => {
    const item = evidenceTemplate.content.cloneNode(true);
    item.querySelector("p").textContent = hit.text;
    const score = Number.isFinite(hit.score) ? ` · ${(hit.score * 100).toFixed(0)}%` : "";
    item.querySelector("small").textContent = `${hit.entry_id} · ${(hit.matched_by || []).join("+")}${score}`;
    evidenceList.append(item);
  });

  if (!hits.length) {
    const empty = document.createElement("li");
    empty.innerHTML = "<div></div><div><p>没有命中相关记忆。</p><small>EMPTY RESULT</small></div>";
    evidenceList.append(empty);
  }

  if (result.context) {
    contextSummary.innerHTML = `<p><strong>PowerContext 组装结果：</strong>${escapeHtml(result.context)}</p>`;
  } else {
    contextSummary.innerHTML = summarize(hits);
  }
  hitCount.textContent = `${hits.length} 条证据`;
  contextSize.textContent = result.context_bytes ? `${result.context_bytes} bytes` : liveMode ? "空上下文" : "浏览器演示结果";
}

resumeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  resumeButton.disabled = true;
  resumeButton.textContent = "检索中…";
  const query = document.querySelector("#query").value.trim();

  try {
    const result = liveMode
      ? await requestJson("/api/resume", { method: "POST", body: JSON.stringify({ query }) })
      : { hits: searchDemo(query), context: null, context_bytes: 0 };
    renderEvidence(result);
  } catch (error) {
    contextSummary.innerHTML = `<p><strong>检索失败：</strong>${escapeHtml(error.message)}</p>`;
  } finally {
    resumeButton.disabled = false;
    resumeButton.textContent = "生成接续包";
  }
});

renderEvidence({ hits: DEMO_MEMORIES, context: null, context_bytes: 0 });
detectMode();
