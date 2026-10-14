const PLUGIN = "astrbot_plugin_daily_vocaloid";

const nativeBridge = window.AstrBotPluginPage;

const bridge = {
  async apiGet(path, params = {}) {
    if (!nativeBridge || typeof nativeBridge.apiGet !== "function") {
      throw new Error(
        "AstrBotPluginPage bridge 不可用。请从 AstrBot 插件管理页打开本页面。"
      );
    }

    let lastError = null;

    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        return await nativeBridge.apiGet(path, params);
      } catch (error) {
        lastError = error;
        if (attempt < 3) {
          await sleep(700 * attempt);
        }
      }
    }

    throw new Error(
      lastError?.message ||
      "插件后端请求失败。AstrBot bridge 已连接，但 API 调用未成功。"
    );
  },

  async apiPost(path, body = {}) {
    if (!nativeBridge || typeof nativeBridge.apiPost !== "function") {
      throw new Error(
        "AstrBotPluginPage bridge 不可用。请从 AstrBot 插件管理页打开本页面。"
      );
    }

    return await nativeBridge.apiPost(path, body);
  },
};

const state = { items: [], total: 0, limit: 50, offset: 0, editingId: null };

const statsEl = document.getElementById("stats");
const songsEl = document.getElementById("songs");
const searchEl = document.getElementById("search");
const languageEl = document.getElementById("language");
const sourceEl = document.getElementById("source");
const levelEl = document.getElementById("level");
const statusEl = document.getElementById("status");
const pageInfoEl = document.getElementById("pageInfo");
const editDialog = document.getElementById("editDialog");

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}


function videoIdLabel(song) {
  if (song.bvid) return song.bvid;

  const match = String(song.bilibili_url || "").match(
    /\/video\/(av\d+)/i
  );
  return match ? match[1] : "—";
}

function formatCount(value) {
  const n = Number(value || 0);
  if (n >= 100000000) return `${(n / 100000000).toFixed(1)}亿`;
  if (n >= 10000) return `${(n / 10000).toFixed(1)}万`;
  return String(n);
}

function levelLabel(level) {
  return { hall: "🏛️ 殿堂", legend: "🔥 传说", myth: "🌟 神话" }[level] || "🏛️ 殿堂";
}

function sourceLabel(source) {
  const s = String(source || "");
  if (s.includes("vocawiki") && s.includes("vcpedia")) return "双源";
  if (s.includes("vocawiki")) return "Vocawiki";
  if (s.includes("vcpedia")) return "VCPedia";
  return "人工";
}

function languageLabel(language) {
  return language === "zh" ? "中文" : "日语 / 外语";
}

function showMessage(text, type = "info", timeout = 0) {
  const el = document.getElementById("message");
  el.hidden = false;
  el.dataset.type = type;
  el.textContent = text;

  if (timeout > 0) {
    setTimeout(() => {
      if (el.textContent === text) el.hidden = true;
    }, timeout);
  }
}

function setProgress(done, total, label) {
  const wrap = document.getElementById("progressWrap");
  wrap.hidden = false;

  const safeTotal = Math.max(Number(total || 0), 1);
  const pct = Math.max(0, Math.min(100, Math.round(Number(done || 0) / safeTotal * 100)));

  document.getElementById("progressBar").style.width = `${pct}%`;
  document.getElementById("progressLabel").textContent = label || "";
  document.getElementById("progressPercent").textContent = `${pct}%`;
}

function statusInfo(song) {
  if (Number(song.manual_override || 0) === 1) {
    return song.available
      ? ["🔒 人工启用", "manual"]
      : ["🔒 人工停用", "manualOff"];
  }

  if (song.available) {
    if (song.bilibili_status === "matched") {
      return ["✅ 可推荐 · 已巡检", "enabled"];
    }
    if (song.bilibili_status === "error") {
      return ["✅ 可推荐 · 巡检异常", "enabled"];
    }
    return ["✅ 可推荐", "enabled"];
  }

  if (!song.cover_url && !song.cover_local) {
    return ["🖼️ 缺百科封面", "pending"];
  }

  return ["○ 未入池", "pending"];
}

async function loadStats() {
  const data = await bridge.apiGet("stats");

  const cards = [
    ["新曲库总数", data.total ?? 0, ""],
    ["日语 / 外语", data.foreign ?? 0, "foreign"],
    ["中文", data.chinese ?? 0, "zh"],
    ["可推荐", data.available ?? 0, "enabled"],
    ["百科封面", data.with_cover ?? 0, "cover"],
    ["缺封面", data.missing_cover ?? 0, ""],
    ["B站已巡检", data.verified ?? 0, "verified"],
    ["未巡检B站", data.pending ?? 0, "pending"],
    ["🏛️ 殿堂", data.levels?.hall ?? 0, "hall"],
    ["🔥 传说", data.levels?.legend ?? 0, "legend"],
    ["🌟 神话", data.levels?.myth ?? 0, "myth"],
    ["验证失败", data.errors ?? 0, "error"],
  ];

  statsEl.innerHTML = cards.map(([label, value, filter]) => `
    <article class="stat ${filter ? "statClickable" : ""}" ${filter ? `data-filter="${escapeHtml(filter)}"` : ""}>
      <div class="statLabel">${escapeHtml(label)}</div>
      <div class="statValue">${escapeHtml(value)}</div>
    </article>
  `).join("");

  statsEl.querySelectorAll("[data-filter]").forEach(card => {
    card.addEventListener("click", () => {
      const f = card.dataset.filter;
      if (f === "foreign" || f === "zh") {
        languageEl.value = f;
        statusEl.value = "";
        levelEl.value = "";
      } else if (["hall", "legend", "myth"].includes(f)) {
        levelEl.value = f;
        statusEl.value = "";
      } else {
        statusEl.value = f;
      }
      loadSongs(true);
    });
  });
}

async function loadSongs(reset = false) {
  if (reset) state.offset = 0;

  const data = await bridge.apiGet("songs", {
    q: searchEl.value.trim(),
    language: languageEl.value,
    source: sourceEl.value,
    level: levelEl.value,
    status: statusEl.value,
    limit: state.limit,
    offset: state.offset,
  });

  state.items = data.items || [];
  state.total = Number(data.total || 0);

  songsEl.innerHTML = state.items.map(song => {
    const [statusLabel, statusClass] = statusInfo(song);

    return `
      <tr class="${song.available ? "rowEnabled" : ""}">
        <td>
          <div class="title">${escapeHtml(song.title_zh || song.title_jp || "-")}</div>
          <div class="sub">${escapeHtml(song.publish_date || "")}</div>
        </td>
        <td>
          <div>${escapeHtml(song.producer || "-")}</div>
          <div class="sub">${escapeHtml(song.catalog_engine || song.vocal || "歌声合成")}</div>
        </td>
        <td>${escapeHtml(languageLabel(song.catalog_language))}</td>
        <td><span class="levelBadge level-${escapeHtml(song.catalog_level || "hall")}">${escapeHtml(levelLabel(song.catalog_level))}</span></td>
        <td>${escapeHtml(sourceLabel(song.source))}</td>
        <td title="${escapeHtml(song.bilibili_last_error || "")}">
          <div>${escapeHtml(videoIdLabel(song))}</div>
          <div class="sub">${escapeHtml(
            song.bilibili_status === "matched"
              ? "百科B站链接 · B站已巡检"
              : song.bilibili_status === "error"
                ? "百科B站链接 · 巡检异常（不影响入池）"
                : "百科B站链接 · 无需B站巡检"
          )}</div>
        </td>
        <td>${escapeHtml(formatCount(song.bilibili_view))}</td>
        <td><span class="statusBadge status-${escapeHtml(statusClass)}">${escapeHtml(statusLabel)}</span></td>
        <td><button class="small" data-edit="${song.id}">编辑</button></td>
      </tr>
    `;
  }).join("");

  songsEl.querySelectorAll("[data-edit]").forEach(btn => {
    btn.addEventListener("click", () => openEditor(Number(btn.dataset.edit)));
  });

  const start = state.total ? state.offset + 1 : 0;
  const end = Math.min(state.offset + state.limit, state.total);
  pageInfoEl.textContent = `${start}-${end} / ${state.total}`;

  document.getElementById("prevBtn").disabled = state.offset <= 0;
  document.getElementById("nextBtn").disabled = state.offset + state.limit >= state.total;
}

async function syncCatalog() {
  const btn = document.getElementById("catalogBtn");
  btn.disabled = true;
  btn.textContent = "同步中…";

  try {
    showMessage(
      "正在建立全新的殿堂曲库：先同步 Vocawiki 日语/外语，再同步 VCPedia 中文。翻唱条目会自动过滤。",
      "info",
      0
    );
    setProgress(0, 1, "准备同步新曲库…");

    await bridge.apiPost("catalog/sync", {});

    while (true) {
      await sleep(1000);
      const s = await bridge.apiGet("catalog/sync/status");

      const sourceText =
        s.current_source === "vocawiki"
          ? "Vocawiki 日语/外语"
          : s.current_source === "vcpedia"
            ? "VCPedia 中文"
            : "曲库";

      const throttleText =
        s.throttle_state === "rate_limited"
          ? ` · 触发 429，暂停 ${Number(s.wait_seconds || 0)} 秒`
          : s.throttle_state === "service_unavailable"
            ? ` · 服务暂不可用，暂停 ${Number(s.wait_seconds || 0)} 秒`
            : s.throttle_state === "backoff"
              ? ` · 网络退避 ${Number(s.wait_seconds || 0)} 秒`
              : s.throttle_state === "source_stopped"
                ? " · 当前来源已安全停止"
                : ` · 请求间隔 ${Number(s.request_interval || 1.3).toFixed(1)} 秒`;

      setProgress(
        Number(s.pages_scanned || 0),
        Math.max(
          Number(s.pages_scanned || 0) + Number(s.pages_queued || 0),
          1
        ),
        `${sourceText}：${s.current_page || ""} · 已发现 ${Number(s.records_found || 0)} 条${throttleText}`
      );

      if (!s.running) {
        const vw = Number(s.vocawiki_records || 0);
        const zh = Number(s.vcpedia_records || 0);
        const total = Number(s.total_records || s.records_found || 0);
        const errors = Number(s.errors || 0);

        const stopped = [
          s.vocawiki_blocked ? "Vocawiki" : "",
          s.vcpedia_blocked ? "VCPedia" : "",
        ].filter(Boolean);

        showMessage(
          `新曲库同步完成：Vocawiki 日/外语 ${vw}，VCPedia 中文 ${zh}，数据库现有 ${total}。新增 ${Number(s.inserted || 0)}，更新 ${Number(s.updated || 0)}，页面错误 ${errors}。` +
          (stopped.length
            ? ` 为避免触发站点保护，已安全停止：${stopped.join("、")}。已抓到的数据已经保存，下次可继续同步。`
            : "") +
          (s.last_error ? ` 最后错误：${s.last_error}` : ""),
          errors ? "error" : "success",
          15000
        );

        await Promise.all([loadStats(), loadSongs(true)]);
        return;
      }
    }
  } catch (error) {
    showMessage(error.message || "曲库同步失败", "error", 0);
  } finally {
    btn.disabled = false;
    btn.textContent = "同步全部曲库";
  }
}

async function verifyBilibili(retry = false) {
  const btn = retry
    ? document.getElementById("retryBtn")
    : document.getElementById("verifyBtn");
  const select = document.getElementById("verifyCount");
  const target = Number(select.value || 20);

  btn.disabled = true;
  select.disabled = true;

  try {
    showMessage(
      retry
        ? "正在重试 B站巡检失败项；这只是可选健康检查，不影响推荐资格。"
        : `正在巡检 ${target} 首百科 BV；只检查存活/播放量，不再负责补封面或入池。`,
      "info",
      0
    );

    await bridge.apiPost("bilibili/verify", { limit: target, retry });

    while (true) {
      await sleep(900);
      const s = await bridge.apiGet("bilibili/verify/status");

      const processed = Number(s.processed || 0);
      const total = Math.max(Number(s.target || target), 1);

      const throttleText =
        s.throttle_state === "rate_limited"
          ? ` · 触发访问保护，暂停 ${Number(s.wait_seconds || 0)} 秒`
          : s.throttle_state === "protection_stop"
            ? " · 已安全停止，稍后可继续"
            : s.throttle_state === "chunk_cooldown"
              ? ` · 分段休息 ${Number(s.wait_seconds || 0)} 秒`
              : s.throttle_state === "network_backoff"
                ? ` · 网络退避 ${Number(s.wait_seconds || 0)} 秒`
                : "";

      setProgress(
        processed,
        total,
        `Bilibili 可选巡检 ${processed}/${total} · 本批日/外 ${Number(s.target_foreign || 0)} / 中文 ${Number(s.target_chinese || 0)}${s.current ? ` · ${s.current}` : ""}${throttleText}`
      );

      if (!s.running) {
        const protection =
          s.stopped_by_protection
            ? " 已检测到 Bilibili 访问保护，本轮已主动停止；已完成结果全部保留，稍后继续即可。"
            : "";

        showMessage(
          `B站巡检完成：成功 ${Number(s.matched || 0)}（日/外 ${Number(s.matched_foreign || 0)}，中文 ${Number(s.matched_chinese || 0)}），失败 ${Number(s.errors || 0)}。剩余待验证 ${Number(s.pending || 0)}（日/外 ${Number(s.pending_foreign || 0)}，中文 ${Number(s.pending_chinese || 0)}）。${protection}${s.last_error ? ` 最后错误：${s.last_error}` : ""}`,
          Number(s.errors || 0) || s.stopped_by_protection ? "error" : "success",
          15000
        );

        await Promise.all([loadStats(), loadSongs(true)]);
        return;
      }
    }
  } catch (error) {
    showMessage(error.message || "Bilibili 验证失败", "error", 0);
  } finally {
    btn.disabled = false;
    select.disabled = false;
  }
}


async function stopBilibiliPatrol() {
  const btn = document.getElementById("stopVerifyBtn");
  btn.disabled = true;
  btn.textContent = "停止中…";

  try {
    const result = await bridge.apiPost(
      "bilibili/verify/cancel",
      {}
    );

    if (result.cancelled) {
      showMessage(
        `B站巡检已停止。此前已经完成的 ${Number(result.processed || 0)} 首结果已保存。`,
        "success",
        8000
      );
    } else {
      showMessage(
        result.message || "当前没有正在运行的 B站巡检。",
        "info",
        5000
      );
    }
  } catch (error) {
    showMessage(error.message || "停止巡检失败", "error", 0);
  } finally {
    btn.disabled = false;
    btn.textContent = "停止巡检";
    await Promise.all([loadStats(), loadSongs(false)]);
  }
}


function syncEditorControlState() {
  const manual = document.getElementById("editControlMode").value === "manual";
  document.getElementById("editPool").disabled = !manual;
  document.getElementById("editAvailable").disabled = !manual;

  document.getElementById("editModeHint").textContent = manual
    ? "人工锁定可覆盖殿堂自动规则；启用至少需要有效 BV。"
    : "自动规则：百科殿堂及以上 + 精确 B站链接验证成功 + 封面缓存成功。";
}

function openEditor(id) {
  const song = state.items.find(x => Number(x.id) === Number(id));
  if (!song) return;

  state.editingId = song.id;

  document.getElementById("editTitle").value = song.title_zh || song.title_jp || "";
  document.getElementById("editProducer").value = song.producer || "";
  document.getElementById("editVocal").value = song.vocal || "";
  document.getElementById("editDate").value = song.publish_date || "";
  document.getElementById("editEngine").value = song.catalog_engine || "";
  document.getElementById("editBvid").value = song.bvid || "";
  document.getElementById("editBilibili").value = song.bilibili_url || "";
  document.getElementById("editCover").value = song.cover_url || "";
  document.getElementById("editPool").value = song.pool_type || "normal";
  document.getElementById("editAvailable").value = song.available ? "1" : "0";
  document.getElementById("editControlMode").value =
    Number(song.manual_override || 0) === 1 ? "manual" : "auto";

  const source = document.getElementById("editSource");
  source.textContent = `${sourceLabel(song.source)} · ${song.catalog_source_title || "人工条目"}`;
  source.dataset.url = song.catalog_source_page || "";
  source.classList.toggle("clickable", Boolean(song.catalog_source_page));

  syncEditorControlState();
  editDialog.showModal();
}

async function saveCurrent() {
  const id = state.editingId;
  if (!id) return;

  const payload = {
    title_jp: document.getElementById("editTitle").value.trim(),
    title_zh: document.getElementById("editTitle").value.trim(),
    producer: document.getElementById("editProducer").value.trim(),
    vocal: document.getElementById("editVocal").value.trim(),
    publish_date: document.getElementById("editDate").value.trim(),
    bvid: document.getElementById("editBvid").value.trim(),
    bilibili_url: document.getElementById("editBilibili").value.trim(),
    cover_url: document.getElementById("editCover").value.trim(),
    pool_type: document.getElementById("editPool").value,
    available: Number(document.getElementById("editAvailable").value),
    manual_override:
      document.getElementById("editControlMode").value === "manual" ? 1 : 0,
  };

  const result = await bridge.apiPost(`songs/${id}/update`, payload);
  editDialog.close();

  showMessage(
    result.manual_override
      ? (result.available ? `已人工启用：${result.bvid}` : "已人工锁定为停用。")
      : "已恢复殿堂自动规则。",
    "success",
    7000
  );

  await Promise.all([loadStats(), loadSongs(false)]);
}

async function deleteCurrent() {
  const id = state.editingId;
  if (!id || !confirm("确定删除这条歌曲吗？")) return;

  await bridge.apiPost(`songs/${id}/delete`, {});
  editDialog.close();
  showMessage("已删除", "success", 4000);
  await Promise.all([loadStats(), loadSongs(true)]);
}

document.getElementById("catalogBtn").addEventListener("click", syncCatalog);
document.getElementById("verifyBtn").addEventListener("click", () => verifyBilibili(false));
document.getElementById("retryBtn").addEventListener("click", () => verifyBilibili(true));
document.getElementById("stopVerifyBtn").addEventListener("click", stopBilibiliPatrol);
document.getElementById("searchBtn").addEventListener("click", () => loadSongs(true));
document.getElementById("refreshBtn").addEventListener("click", () => Promise.all([loadStats(), loadSongs(false)]));

[languageEl, sourceEl, levelEl, statusEl].forEach(el => {
  el.addEventListener("change", () => loadSongs(true));
});
searchEl.addEventListener("keydown", event => {
  if (event.key === "Enter") loadSongs(true);
});

document.getElementById("prevBtn").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - state.limit);
  loadSongs(false);
});
document.getElementById("nextBtn").addEventListener("click", () => {
  if (state.offset + state.limit < state.total) {
    state.offset += state.limit;
    loadSongs(false);
  }
});

document.getElementById("editControlMode").addEventListener("change", syncEditorControlState);
document.getElementById("saveBtn").addEventListener("click", saveCurrent);
document.getElementById("deleteBtn").addEventListener("click", deleteCurrent);
document.getElementById("cancelBtn").addEventListener("click", () => editDialog.close());
document.getElementById("closeDialogBtn").addEventListener("click", () => editDialog.close());
document.getElementById("editSource").addEventListener("click", event => {
  const url = event.currentTarget.dataset.url;
  if (url) window.open(url, "_blank", "noopener,noreferrer");
});

async function boot() {
  showMessage("正在连接插件后端…", "info", 0);

  let health = null;
  let lastError = null;

  for (let attempt = 1; attempt <= 8; attempt++) {
    try {
      health = await bridge.apiGet("health");
      break;
    } catch (error) {
      lastError = error;
      showMessage(
        `插件后端尚未就绪，正在重试 ${attempt}/8…`,
        "info",
        0
      );
      await sleep(1500);
    }
  }

  if (!health?.ok) {
    showMessage(
      `${lastError?.message || "无法连接插件后端"} 已通过 AstrBot 插件页面桥接请求后端；请检查插件日志中是否有 API 注册或运行时异常。`,
      "error",
      0
    );
    return;
  }

  try {
    await Promise.all([loadStats(), loadSongs(true)]);
    document.getElementById("message").hidden = true;
  } catch (error) {
    showMessage(error.message || "页面加载失败", "error", 0);
  }
}

boot();
