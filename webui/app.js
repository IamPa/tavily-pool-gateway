/* Tavily Pool Gateway Console — 页面逻辑（同源 fetch，浏览器/Tauri 通用） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const PAGE_TITLES = { overview: "概览", search: "搜索测试", keys: "密钥管理", guide: "接入指南" };
const STATUS_TEXT = { healthy: "健康", cooling: "冷却中", exhausted: "额度耗尽", disabled: "已禁用" };

/* 演示模式：/ui/?demo=1 时展示假数据（仓库展示/无 key 体验用，不请求网关） */
const DEMO = new URLSearchParams(location.search).has("demo");
const DEMO_POOL = {
  keys: [
    { name: "key1", status: "healthy", credits_used: 812, credits_limit: 1000, credits_remaining: 188, consecutive_429: 0, cooldown_seconds_left: 0, billing_month: "2026-08", last_error: null },
    { name: "key2", status: "healthy", credits_used: 341, credits_limit: 1000, credits_remaining: 659, consecutive_429: 0, cooldown_seconds_left: 0, billing_month: "2026-08", last_error: null },
    { name: "key3", status: "healthy", credits_used: 97, credits_limit: 1000, credits_remaining: 903, consecutive_429: 0, cooldown_seconds_left: 0, billing_month: "2026-08", last_error: null },
    { name: "key4", status: "cooling", credits_used: 640, credits_limit: 1000, credits_remaining: 360, consecutive_429: 2, cooldown_seconds_left: 96, billing_month: "2026-08", last_error: "RPM 限流 (429)" },
    { name: "key5", status: "exhausted", credits_used: 1000, credits_limit: 1000, credits_remaining: 0, consecutive_429: 0, cooldown_seconds_left: 0, billing_month: "2026-08", last_error: "配额耗尽 (429)" },
  ],
};
const DEMO_SEARCH = {
  query: "demo", response_time: 1.2,
  results: [
    { title: "演示结果一：示例文档页面", url: "https://example.com/demo-doc", content: "这是演示模式下的示例搜索结果，用于展示界面的呈现样式。配置真实 key 后，这里会显示 Tavily 的实际搜索结果。", score: 0.92 },
    { title: "演示结果二：另一篇示例文章", url: "https://example.org/article", content: "搜索结果卡片包含标题、链接、内容摘要与相关度评分。演示数据不消耗任何 API 额度。", score: 0.87 },
  ],
  usage: { credits: 1 },
};

let refreshTimer = null;

/* ---------------- 导航 ---------------- */
$$(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".nav-item").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const page = btn.dataset.page;
    $$(".page").forEach((p) => p.classList.remove("active"));
    $("#page-" + page).classList.add("active");
    $("#page-title").textContent = PAGE_TITLES[page];
  });
});

/* ---------------- 健康与池状态 ---------------- */
async function checkHealth() {
  const dot = $("#conn-dot");
  const text = $("#conn-text");
  if (DEMO) {
    dot.className = "conn-dot ok";
    text.textContent = "演示模式";
    $("#gateway-tag").textContent = "演示数据";
    return true;
  }
  try {
    const r = await fetch("/health", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    dot.className = "conn-dot ok";
    text.textContent = "已连接";
    $("#offline-banner").classList.add("hidden");
    $("#gateway-tag").textContent = "网关就绪";
    return true;
  } catch (e) {
    dot.className = "conn-dot err";
    text.textContent = "未连接";
    $("#offline-banner").classList.remove("hidden");
    $("#gateway-tag").textContent = "网关不可达";
    return false;
  }
}

function fmt(n) {
  return typeof n === "number" ? n.toLocaleString("en-US") : "--";
}

function renderPool(data) {
  const keys = data.keys || [];
  const healthy = keys.filter((k) => k.status === "healthy");
  const totalRemaining = healthy.reduce((s, k) => s + k.credits_remaining, 0);
  const totalUsed = keys.reduce((s, k) => s + k.credits_used, 0);
  const totalLimit = keys.reduce((s, k) => s + k.credits_limit, 0);

  $("#total-remaining-mini").textContent = fmt(totalRemaining);
  $("#stat-total").textContent = fmt(totalRemaining);
  $("#stat-total-sub").textContent = `健康 key 合计（上限 ${fmt(totalLimit)}）`;
  $("#stat-keys").textContent = keys.length;
  $("#stat-keys-total").textContent = "/" + healthy.length + " 健康";
  const months = [...new Set(keys.map((k) => k.billing_month))];
  $("#stat-month").textContent = "计费月 " + (months.join(", ") || "--");
  $("#stat-used").textContent = fmt(totalUsed);

  // 空池：隐藏统计卡与 key 卡片，显示居中引导卡
  const isEmpty = keys.length === 0;
  $("#empty-state").classList.toggle("hidden", !isEmpty);
  document.querySelector("#page-overview .stat-row").classList.toggle("hidden", isEmpty);

  // 概览卡片
  if (isEmpty) return;
  const cards = keys
    .map((k) => {
      const pct = k.credits_limit ? Math.round((k.credits_remaining / k.credits_limit) * 100) : 0;
      const barCls = pct <= 15 ? "low" : pct <= 40 ? "mid" : "";
      const err = k.last_error ? `<div class="key-err">${escapeHtml(k.last_error)}</div>` : "";
      const cool = k.cooldown_seconds_left > 0 ? ` · 冷却 ${k.cooldown_seconds_left}s` : "";
      return `
      <div class="key-card">
        <div class="key-card-head">
          <span class="key-name">${escapeHtml(k.name)}</span>
          <span class="badge ${k.status}">${STATUS_TEXT[k.status] || k.status}${cool}</span>
        </div>
        <div class="key-remaining">
          <span class="key-remaining-num">${fmt(k.credits_remaining)}</span>
          <span class="key-remaining-unit">/ ${fmt(k.credits_limit)} credits 剩余</span>
        </div>
        <div class="progress"><div class="progress-bar ${barCls}" style="width:${pct}%"></div></div>
        <div class="key-meta"><span>已用 ${fmt(k.credits_used)}</span><span>剩余 ${pct}%</span></div>
        ${err}
      </div>`;
    })
    .join("");
  $("#key-cards").innerHTML = cards;

  // 密钥管理表格
  const rows = keys
    .map(
      (k) => `
      <tr>
        <td>${escapeHtml(k.name)}</td>
        <td><span class="badge ${k.status}">${STATUS_TEXT[k.status] || k.status}</span></td>
        <td>${fmt(k.credits_used)} / ${fmt(k.credits_limit)}</td>
        <td>${fmt(k.credits_remaining)}</td>
        <td class="td-dim">${k.credits_limit ? Math.round((k.credits_used / k.credits_limit) * 100) : 0}%</td>
        <td class="${k.last_error ? "td-err" : "td-dim"}">${escapeHtml(k.last_error || "-")}</td>
        <td><button class="btn btn-sm-danger" data-del="${escapeHtml(k.name)}">删除</button></td>
      </tr>`
    )
    .join("");
  $("#keys-tbody").innerHTML = rows || '<tr><td colspan="6" class="td-dim">暂无 key。在上方粘贴添加，或使用 /admin/keys 接口。</td></tr>';

  // 绑定删除按钮（委托）
  $$("#keys-tbody [data-del]").forEach((btn) => {
    btn.addEventListener("click", () => delKey(btn.dataset.del));
  });
}

async function delKey(name) {
  if (DEMO) { alert("演示模式：删除操作不可用"); return; }
  if (!confirm(`确定删除 ${name} 吗？该 key 将从池中和 api.txt 中一并移除。`)) return;
  try {
    const r = await fetch("/admin/keys/" + encodeURIComponent(name), { method: "DELETE" });
    const data = await r.json();
    if (r.ok) {
      refreshPool();
    } else {
      alert("删除失败：" + ((data.detail && data.detail.error) || "HTTP " + r.status));
    }
  } catch (e) {
    alert("请求异常：" + e.message);
  }
}

async function refreshPool() {
  if (DEMO) {
    renderPool(DEMO_POOL);
    return;
  }
  try {
    const r = await fetch("/admin/pool", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    renderPool(await r.json());
  } catch (e) {
    /* 健康检查已处理离线展示 */
  }
}

async function refreshAll() {
  const ok = await checkHealth();
  if (ok) await refreshPool();
}

/* ---------------- 搜索测试 ---------------- */
async function doSearch() {
  const q = $("#search-query").value.trim();
  const btn = $("#btn-search");
  const meta = $("#search-meta");
  if (!q) return;
  btn.disabled = true;
  btn.textContent = "搜索中…";
  meta.classList.add("hidden");
  $("#search-results").innerHTML = "";
  const t0 = performance.now();
  if (DEMO) {
    await new Promise((res) => setTimeout(res, 600));
    const data = JSON.parse(JSON.stringify(DEMO_SEARCH));
    data.query = q;
    const ms = Math.round(performance.now() - t0);
    renderSearch(data, ms);
    return;
  }
  try {
    const r = await fetch("/v1/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: q,
        max_results: parseInt($("#search-max").value, 10),
        search_depth: $("#search-depth").value,
      }),
    });
    const data = await r.json();
    const ms = Math.round(performance.now() - t0);
    if (!r.ok) {
      const msg = (data && data.detail && data.detail.error) || "HTTP " + r.status;
      meta.textContent = "搜索失败：" + msg;
      meta.classList.remove("hidden");
      return;
    }
    renderSearch(data, ms);
    refreshPool(); // 搜索后刷新记账
  } catch (e) {
    meta.textContent = "请求异常：" + e.message;
    meta.classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = "搜索";
  }
}

function renderSearch(data, ms) {
  const meta = $("#search-meta");
  const credits = data.usage && data.usage.credits;
  const results = data.results || [];
  meta.textContent = `耗时 ${ms}ms · 消耗 ${credits != null ? credits : "?"} credits · ${results.length} 条结果`;
  meta.classList.remove("hidden");
  $("#search-results").innerHTML = results
    .map(
      (item) => `
      <div class="result-card">
        <span class="result-score">score ${item.score != null ? Number(item.score).toFixed(2) : "-"}</span>
        <div class="result-title"><a href="${escapeHtml(item.url)}" target="_blank" rel="noopener">${escapeHtml(item.title || item.url)}</a></div>
        <div class="result-url">${escapeHtml(item.url)}</div>
        <div class="result-content">${escapeHtml((item.content || "").slice(0, 300))}${(item.content || "").length > 300 ? "…" : ""}</div>
      </div>`
    )
    .join("");
}
$("#btn-search").addEventListener("click", doSearch);
$("#search-query").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doSearch();
});

/* ---------------- 添加 key ---------------- */
$("#btn-add-keys").addEventListener("click", async () => {
  const msg = $("#add-keys-msg");
  const raw = $("#add-keys-input").value
    .split("\n")
    .map((s) => s.trim())
    .filter((s) => s && !s.startsWith("#"));
  if (!raw.length) {
    msg.textContent = "请输入至少一个 key";
    msg.className = "form-msg err";
    return;
  }
  try {
    const r = await fetch("/admin/keys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keys: raw }),
    });
    const data = await r.json();
    if (r.ok) {
      msg.textContent = `已添加 ${data.added} 个（池内共 ${data.total_keys} 个）`;
      msg.className = "form-msg ok";
      $("#add-key-input").value = "";
      refreshPool();
    } else {
      msg.textContent = "失败：" + ((data.detail && data.detail.error) || "HTTP " + r.status);
      msg.className = "form-msg err";
    }
  } catch (e) {
    msg.textContent = "请求异常：" + e.message;
    msg.className = "form-msg err";
  }
});

/* ---------------- 复制 ---------------- */
$$(".btn-copy").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const text = document.getElementById(btn.dataset.copy).textContent;
    try {
      await navigator.clipboard.writeText(text.trim());
      btn.textContent = "已复制";
    } catch (e) {
      btn.textContent = "复制失败";
    }
    setTimeout(() => (btn.textContent = "复制"), 1500);
  });
});

/* ---------------- 空状态引导 ---------------- */
$("#btn-empty-add").addEventListener("click", () => {
  document.querySelector('button.nav-item[data-page="keys"]').click();
});

/* ---------------- 手动刷新 ---------------- */
$("#btn-refresh").addEventListener("click", refreshAll);

/* ---------------- 工具 ---------------- */
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------------- 启动 ---------------- */
refreshAll();
refreshTimer = setInterval(refreshAll, 10000);
