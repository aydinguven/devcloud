(() => {
  const C = window.GenAiCharts;
  const $ = (id) => document.getElementById(id);
  const METRIC_LABELS = {total_tokens: "Token", api_requests: "İstek", spend: "Harcama"};
  const MEDALS = ["🥇", "🥈", "🥉"];
  let stats = null;
  let days = 30;
  let metric = "total_tokens";

  const pct = (part, whole) => (whole ? `%${((part / whole) * 100).toFixed(1)}` : "—");
  const value = (row) => Number(row[metric]) || 0;

  function board(listId, rows, {name, detail, emptyId}) {
    const list = $(listId);
    const sorted = rows.filter((row) => value(row) > 0).sort((a, b) => value(b) - value(a)).slice(0, 10);
    if (emptyId) $(emptyId).hidden = sorted.length > 0;
    const top = sorted.length ? value(sorted[0]) : 1;
    const fmt = C.formatter(metric);
    list.replaceChildren(...sorted.map((row, index) => {
      const item = document.createElement("li");
      item.className = `genai-board-row${index < 3 ? " is-podium" : ""}`;
      const rank = document.createElement("span");
      rank.className = "genai-board-rank";
      rank.textContent = MEDALS[index] || String(index + 1);
      const body = document.createElement("div");
      body.className = "genai-board-body";
      const head = document.createElement("div");
      head.className = "genai-board-head";
      const title = document.createElement("strong");
      title.textContent = name(row);
      const amount = document.createElement("span");
      amount.textContent = fmt(value(row));
      head.append(title, amount);
      const track = document.createElement("div");
      track.className = "genai-board-track";
      const fill = document.createElement("span");
      fill.style.width = `${Math.max(2, (value(row) / top) * 100)}%`;
      fill.style.background = C.PALETTE[index % C.PALETTE.length];
      track.append(fill);
      const meta = document.createElement("small");
      meta.textContent = detail(row);
      body.append(head, track, meta);
      item.append(rank, body);
      return item;
    }));
    if (!sorted.length && !emptyId) {
      const item = document.createElement("li");
      item.className = "text-muted";
      item.textContent = "Bu dönemde kullanım yok.";
      list.append(item);
    }
  }

  function render() {
    const t = stats.totals;
    $("kpi-requests").textContent = C.compact(t.api_requests);
    $("kpi-success").textContent = `${pct(t.successful_requests, t.api_requests)} başarılı · ${C.compact(t.failed_requests)} hatalı`;
    $("kpi-tokens").textContent = C.compact(t.total_tokens);
    $("kpi-tokens-split").textContent = `${C.compact(t.prompt_tokens)} girdi · ${C.compact(t.completion_tokens)} çıktı`;
    $("kpi-users").textContent = String(t.active_users);
    $("kpi-per-user").textContent = t.active_users ? `kişi başı ${C.compact(t.total_tokens / t.active_users)} token` : "";
    if (stats.show_spend) {
      $("kpi-spend").textContent = C.money(t.spend);
      $("kpi-spend-day").textContent = `günlük ort. ${C.money(t.spend / stats.days)}`;
    }
    $("genai-stats-range").textContent = `${stats.start} → ${stats.end}`;

    const labels = stats.daily.map((day) => day.date);
    const lineMetric = metric === "api_requests" ? "total_tokens" : "api_requests";
    C.bars($("chart-daily"), {
      labels,
      metric,
      series: [{name: METRIC_LABELS[metric], color: "#d50032", values: stats.daily.map(value)}],
      line: {name: METRIC_LABELS[lineMetric], metric: lineMetric, color: "#2563eb", values: stats.daily.map((day) => day[lineMetric] || 0)},
    });
    $("daily-legend").textContent = `■ ${METRIC_LABELS[metric]}  ─ ${METRIC_LABELS[lineMetric]}`;

    $("users-count").textContent = `${stats.users.length} kullanıcı`;
    board("board-users", stats.users, {
      name: (row) => row.full_name ? `${row.username} · ${row.full_name}` : row.username,
      detail: (row) => `${C.compact(row.api_requests)} istek · ${C.compact(row.total_tokens)} token${row.team ? ` · ${row.team}` : ""}`,
      emptyId: stats.user_breakdown ? null : "board-users-empty",
    });
    board("board-groups", stats.groups, {
      name: (row) => row.name,
      detail: (row) => `${row.members} aktif kullanıcı · ${C.compact(row.api_requests)} istek`,
    });
    board("board-teams", stats.teams || [], {
      name: (row) => row.name,
      detail: (row) => `${C.compact(row.api_requests)} istek · ${C.compact(row.total_tokens)} token`,
      emptyId: "board-teams-empty",
    });

    const models = stats.models.filter((row) => value(row) > 0).slice(0, 8);
    const rest = stats.models.slice(8).reduce((sum, row) => sum + value(row), 0);
    const items = models.map((row, index) => ({label: row.model, value: value(row), color: C.PALETTE[index]}));
    if (rest > 0) items.push({label: "Diğer", value: rest, color: "#94a3b8"});
    C.donut($("chart-models"), items, {metric});
    const total = items.reduce((sum, item) => sum + item.value, 0);
    $("legend-models").replaceChildren(...items.map((item) => {
      const li = document.createElement("li");
      const dot = document.createElement("i");
      dot.style.background = item.color;
      const label = document.createElement("span");
      label.textContent = item.label;
      const share = document.createElement("b");
      share.textContent = pct(item.value, total);
      li.append(dot, label, share);
      return li;
    }));

    const series = (stats.team_series || []).slice(0, 6).map((team, index) => ({
      name: team.name,
      color: C.PALETTE[index % C.PALETTE.length],
      values: team.values.map(value),
    }));
    $("teams-trend-card").hidden = !series.some((s) => s.values.some(Boolean));
    if (!$("teams-trend-card").hidden) {
      C.bars($("chart-teams"), {labels, metric, series});
      $("teams-legend").replaceChildren(...series.map((s) => {
        const span = document.createElement("span");
        span.className = "genai-legend-inline";
        const dot = document.createElement("i");
        dot.style.background = s.color;
        span.append(dot, document.createTextNode(s.name));
        return span;
      }));
    }
  }

  async function load() {
    $("genai-stats-status").hidden = false;
    $("genai-stats-status").textContent = "İstatistikler yükleniyor...";
    try {
      const response = await fetch(`/api/genai/stats?days=${days}`, {credentials: "same-origin"});
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || `İstek başarısız (${response.status})`);
      if (!result.available) throw new Error(`LiteLLM'den veri alınamadı: ${result.error || "bilinmeyen hata"}`);
      stats = result;
      document.querySelectorAll("[data-admin-only]").forEach((node) => { node.hidden = !stats.show_spend; });
      if (!stats.show_spend && metric === "spend") metric = "total_tokens";
      render();
      $("genai-stats-body").hidden = false;
      $("genai-stats-status").hidden = true;
    } catch (error) {
      $("genai-stats-status").textContent = error.message;
    }
  }

  document.querySelectorAll("[data-days]").forEach((button) => button.addEventListener("click", () => {
    days = Number(button.dataset.days);
    document.querySelectorAll("[data-days]").forEach((b) => b.classList.toggle("active", b === button));
    load();
  }));
  document.querySelectorAll("[data-metric]").forEach((button) => button.addEventListener("click", () => {
    metric = button.dataset.metric;
    document.querySelectorAll("[data-metric]").forEach((b) => b.classList.toggle("active", b === button));
    if (stats) render();
  }));
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (stats) render(); }, 150);
  });
  load();
})();
