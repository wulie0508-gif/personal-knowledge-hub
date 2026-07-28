const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const notice = (element, message, error = false) => {
  element.hidden = false;
  element.textContent = message;
  element.classList.toggle("error", error);
};

const post = async (url, body = {}) => {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "操作失败");
  return result;
};

const setPill = (element, label, state = "") => {
  element.textContent = label;
  element.className = `status-pill ${state}`;
};

let loginPoll = null;

async function refreshLogin() {
  const state = await fetch("/api/archiver-login", { cache: "no-store" }).then((r) => r.json());
  const panel = $("#qrPanel");
  const image = $("#qrImage");
  $("#qrTitle").textContent = {
    starting: "正在生成二维码…",
    waiting: "请用微信扫码",
    success: "登录成功",
    error: "登录没有完成",
    idle: "尚未开始",
  }[state.status] || "扫码登录";
  $("#qrHint").textContent = state.message || "";
  if (state.qr_available) {
    image.src = `/api/archiver-qr?t=${Date.now()}`;
    image.hidden = false;
  } else {
    image.removeAttribute("src");
    image.hidden = true;
  }
  if (state.status === "success") {
    setPill($("#archiverStatus"), "已登录", "ready");
    if (loginPoll) clearInterval(loginPoll);
    loginPoll = null;
  }
  if (state.status === "error" && loginPoll) {
    clearInterval(loginPoll);
    loginPoll = null;
  }
  return state;
}

async function loadStatus() {
  try {
    const status = await fetch("/api/status", { cache: "no-store" }).then((r) => r.json());
    const sts = status.share_to_save;
    setPill($("#stsStatus"), sts.installed && sts.enabled ? "已安装" : "待启用", sts.installed && sts.enabled ? "ready" : "off");
    $("#stsDetail").textContent = `待处理 ${sts.queue_count} 条 · ${sts.output}`;

    const archiver = status.archiver;
    setPill($("#archiverStatus"), archiver.configured ? "已登录" : "待扫码", archiver.configured ? "ready" : "");

    const router = status.router;
    const routerReady = router.monitor_enabled && router.monitor_ready;
    const routerFailed = router.monitor_status === "error";
    setPill(
      $("#routerStatus"),
      routerReady ? "浏览历史监控中" : routerFailed ? "微信读取失败" : "建立基线中",
      routerReady ? "safe" : routerFailed ? "off" : "",
    );
    $("#routerDetail").textContent =
      routerFailed
        ? `${router.monitor_error} 网页直接提交仍可使用。`
        : `只读取新浏览的公众号文章 · ${router.interval_seconds} 秒检查 · 已送入 ${router.submitted_count} 条 · 待处理 ${router.pending_count} 条`;

    const knowledge = status.knowledge;
    const worker = knowledge.ocr_worker;
    const knowledgeReady = knowledge.status === "passed" && knowledge.failed_count === 0;
    setPill(
      $("#knowledgeStatus"),
      knowledgeReady ? (worker.status === "running" ? "OCR 后台处理中" : "验收通过") : "需要检查",
      knowledgeReady ? "ready" : "off",
    );
    const sizeMb = (knowledge.asset_bytes / 1024 / 1024).toFixed(2);
    const preferences = knowledge.preferences || {};
    const graph = knowledge.graph || {};
    const priorities = knowledge.priority_counts || {};
    const curation = knowledge.curation || {};
    const provider = worker.provider || {};
    const workerText = worker.status === "running"
      ? ` · 正在识别：${worker.current_title} · 队列 ${worker.queue_count}`
      : worker.status === "waiting_for_codex"
        ? ` · Codex 待识别 ${worker.queue_count} 篇 · ${provider.message || "等待下一次自动处理"}`
      : worker.last_error
        ? ` · OCR 异常：${worker.last_error}`
        : "";
    const maturityLabels = {
      accumulating: "积累期",
      structuring: "结构化期",
      coverage_gap: "补缺期",
      saturated: "已饱和",
    };
    const graphCount = graph.core_article_count || graph.article_count || knowledge.note_count;
    $("#knowledgeDetail").textContent =
      `知识星球 ${graphCount} 个有效节点 · 原文证据 ${graph.archive_count || 0} 篇 · 成熟度 ${maturityLabels[graph.maturity_stage] || "待评估"} · ` +
      `微信验收 ${knowledge.passed_count}/${knowledge.note_count} 篇 · OCR 待处理 ${knowledge.ocr_pending_count} 篇 · ` +
      `低质量待审 ${knowledge.low_quality_count} 篇 · 本地图片 ${knowledge.asset_count} 张 / ${sizeMb} MB · ` +
      `重点 ${priorities["重点"] || 0} · 速览 ${priorities["速览"] || 0} · 待清理 ${priorities["回收建议"] || 0} · ` +
      `Codex 待整理 ${curation.pending_count || 0} · ` +
      `自动关联 ${graph.link_count || 0} 条 / ${graph.topic_count || 0} 个主题 · ` +
      `已学习 ${preferences.feedback_count || 0} 次（回收区 ${preferences.trash_learned_count || 0}）${workerText}`;
  } catch (error) {
    setPill($("#stsStatus"), "服务异常", "off");
    setPill($("#knowledgeStatus"), "服务异常", "off");
  }
}

const stateLabels = {
  pending: "等待中",
  running: "提取中",
  complete: "已完成",
  attention: "需留意",
  failed: "失败",
  removed: "已回收",
};

async function loadJobs() {
  const data = await fetch("/api/jobs", { cache: "no-store" }).then((r) => r.json());
  const jobs = data.jobs || [];
  $("#jobCount").textContent = `${jobs.length} 条`;
  const list = $("#jobList");
  if (!jobs.length) {
    list.innerHTML = '<div class="empty">还没有任务。粘贴第一条链接试试。</div>';
    return;
  }
  list.innerHTML = jobs.map((job) => {
    const host = (() => { try { return new URL(job.url).hostname; } catch { return job.kind; } })();
    const detail = job.warnings?.length ? job.warnings[0] : job.message;
    const quality = job.quality || null;
    const qualityReasons = quality?.flags?.length ? ` · ${quality.flags.join(" / ")}` : "";
    const valueLine = quality?.knowledge_type
      ? `<small class="value-line">知识价值：${escapeHtml(quality.knowledge_type)} · ${escapeHtml(quality.knowledge_priority)} ${quality.knowledge_value_score}/100 · ${escapeHtml(quality.mastery_status || "未学习")}</small>`
      : "";
    const qualityLine = quality
      ? `<small class="quality-line">分类：${escapeHtml(quality.category)} · 质量：${escapeHtml(quality.tier)} ${quality.score}/100${escapeHtml(qualityReasons)}</small>`
      : "";
    const created = new Date(job.created_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    const canFeedback = job.kind === "wechat_mp" && ["complete", "attention"].includes(job.status);
    const feedback = job.feedback === "keep"
      ? '<span class="learned-tag">已学习：保留</span>'
      : job.feedback === "focus"
        ? '<span class="learned-tag focus">已标记：重点</span>'
        : job.feedback === "mastered"
          ? '<span class="learned-tag mastered">已标记：学会</span>'
      : job.feedback === "auto_remove"
        ? '<span class="learned-tag removed">按偏好自动回收</span>'
        : "";
    const actions = canFeedback ? `
      <div class="job-actions">
        <button class="feedback-button focus" data-job-id="${escapeHtml(job.id)}" data-feedback="focus">设为重点</button>
        <button class="feedback-button mastered" data-job-id="${escapeHtml(job.id)}" data-feedback="mastered">已学会</button>
        <button class="feedback-button remove" data-job-id="${escapeHtml(job.id)}" data-feedback="remove">不需要 · 移入回收区</button>
        ${feedback}
      </div>` : feedback;
    return `
      <article class="job">
        <span class="job-state ${job.status}">${stateLabels[job.status] || job.status}</span>
        <div class="job-main">
          <strong title="${escapeHtml(job.url)}">${escapeHtml(job.title || host)}</strong>
          <small title="${escapeHtml(detail || "")}">${escapeHtml(detail || "")}</small>
          ${qualityLine}
          ${valueLine}
          ${actions}
        </div>
        <time>${created}</time>
      </article>`;
  }).join("");
}

$("#jobList").addEventListener("click", async (event) => {
  const button = event.target.closest(".feedback-button");
  if (!button) return;
  const jobId = button.dataset.jobId;
  const label = button.dataset.feedback;
  button.disabled = true;
  const original = button.textContent;
  button.textContent = label === "remove" ? "正在移入回收区…" : "正在标记…";
  try {
    await post(`/api/jobs/${encodeURIComponent(jobId)}/feedback`, { label });
    await Promise.all([loadJobs(), loadStatus()]);
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    alert(error.message);
  }
});

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function corpusTierLabel(item) {
  const namespace = item.identity?.namespace || item.corpus_namespace;
  if (namespace === "personal_memory") return "个人第二大脑";
  if (namespace === "professional_reference") return "专业研究脑";
  if (namespace === "enterprise_internal") return "企业知识脑";
  if (namespace === "authoritative_external") return "权威外部资料";
  if (namespace === "source_archive") return "原文证据";
  return {
    personal: "个人语料",
    core: "重点知识",
    reference: "参考知识",
    brief: "时效速览",
    evidence: "原文证据",
  }[item.tier] || "本地知识";
}

async function searchKnowledge() {
  const query = $("#knowledgeQuery").value.trim();
  const scope = $("#knowledgeScope").value;
  const target = $("#searchResults");
  if (!query) {
    target.innerHTML = "";
    return;
  }
  target.innerHTML = '<div class="empty">正在检索本地索引…</div>';
  try {
    const payload = await fetch(`/api/search?q=${encodeURIComponent(query)}&scope=${encodeURIComponent(scope)}`, { cache: "no-store" }).then((r) => r.json());
    const results = payload.results || [];
    target.innerHTML = results.length ? results.map((item) => `
      <article class="search-result">
        <strong>${escapeHtml(item.title)}</strong>
        <small>${escapeHtml(corpusTierLabel(item))} · ${escapeHtml(item.category)} · ${escapeHtml(item.account)} · ${Number(item.quality || item.value_score || 0)}/100</small>
        ${item.identity?.represents_user ? '<small>代表你的已确认语料</small>' : '<small>外部/组织资料，不代表用户立场</small>'}
        <p>${escapeHtml(item.snippet || "")}</p>
      </article>
    `).join("") : '<div class="empty">没有找到匹配内容。</div>';
  } catch (error) {
    target.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
  }
}

$$('input[name="route"]').forEach((input) => {
  input.addEventListener("change", () => {
    $$(".route").forEach((label) => label.classList.toggle("active", label.contains($('input[name="route"]:checked'))));
  });
});

$("#submitBtn").addEventListener("click", async () => {
  const text = $("#linkInput").value.trim();
  if (!text) {
    notice($("#submitNotice"), "先粘贴一条链接。", true);
    return;
  }
  const button = $("#submitBtn");
  button.disabled = true;
  button.querySelector("span").textContent = "正在接收…";
  try {
    const result = await post("/api/submit", {
      text,
      route: $('input[name="route"]:checked').value,
      subscribe: $("#subscribe").checked,
      interval: 360,
    });
    notice($("#submitNotice"), `已接收 ${result.jobs.length} 条，后台正在处理。`);
    $("#linkInput").value = "";
    await loadJobs();
    setTimeout(loadJobs, 1800);
    setTimeout(() => { loadJobs(); loadStatus(); }, 6000);
  } catch (error) {
    notice($("#submitNotice"), error.message, true);
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "交给知识库";
  }
});

$("#localImportBtn").addEventListener("click", async () => {
  const path = $("#localPath").value.trim();
  if (!path) {
    notice($("#localNotice"), "先粘贴一个本地文件或文件夹路径。", true);
    return;
  }
  const button = $("#localImportBtn");
  button.disabled = true;
  button.querySelector("span").textContent = "正在读取…";
  try {
    const result = await post("/api/local-import", { path });
    notice($("#localNotice"), "路径已接收，正在提取并接入知识星球。");
    $("#localPath").value = "";
    await loadJobs();
    setTimeout(loadJobs, 1800);
    setTimeout(() => { loadJobs(); loadStatus(); }, 6000);
  } catch (error) {
    notice($("#localNotice"), error.message, true);
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "读入知识星球";
  }
});

$("#linkInput").addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") $("#submitBtn").click();
});
$("#localPath").addEventListener("keydown", (event) => {
  if (event.key === "Enter") $("#localImportBtn").click();
});

$("#openVault").addEventListener("click", () => post("/api/actions/open-vault").catch((e) => alert(e.message)));
$("#refreshStatus").addEventListener("click", () => { loadStatus(); loadJobs(); });
$("#archiverLogin").addEventListener("click", async () => {
  try {
    $("#qrPanel").hidden = false;
    $("#qrTitle").textContent = "正在生成二维码…";
    $("#qrHint").textContent = "正在连接微信公众平台，请稍候。";
    await post("/api/actions/archiver-login");
    await refreshLogin();
    if (loginPoll) clearInterval(loginPoll);
    loginPoll = setInterval(refreshLogin, 1800);
  } catch (error) {
    $("#qrPanel").hidden = false;
    $("#qrTitle").textContent = "无法生成二维码";
    $("#qrHint").textContent = error.message;
  }
});
$("#hideQr").addEventListener("click", () => {
  $("#qrPanel").hidden = true;
});
$("#knowledgeSearch").addEventListener("click", searchKnowledge);
$("#knowledgeQuery").addEventListener("keydown", (event) => {
  if (event.key === "Enter") searchKnowledge();
});

loadStatus();
loadJobs();
setInterval(loadJobs, 5000);
setInterval(loadStatus, 15000);
