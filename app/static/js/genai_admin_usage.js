// Fill admin user cards, teams and müdürlüks with LiteLLM usage (30 days).
(() => {
  const userBlocks = document.querySelectorAll("[data-genai-user]");
  const groupBlocks = document.querySelectorAll("[data-genai-group]");
  const unitBlocks = document.querySelectorAll("[data-genai-unit]");
  if (!userBlocks.length && !groupBlocks.length && !unitBlocks.length) return;
  const C = window.GenAiCharts;

  async function load() {
    let stats;
    try {
      const response = await fetch("/api/genai/stats?days=30", {credentials: "same-origin"});
      stats = await response.json();
      if (!response.ok || !stats.available) return; // GenAI not configured or LiteLLM down
    } catch (_error) {
      return;
    }
    const ranked = stats.users.slice().sort((a, b) => b.total_tokens - a.total_tokens);
    const byUser = new Map(stats.users.filter((row) => row.devcloud_user_id).map((row) => [String(row.devcloud_user_id), row]));
    const byGroup = new Map(stats.groups.map((row) => [row.key, row]));

    userBlocks.forEach((block) => {
      const row = byUser.get(block.dataset.genaiUser);
      const set = (name, text) => { block.querySelector(`[data-llm="${name}"]`).textContent = text; };
      if (row) {
        set("api_requests", C.compact(row.api_requests));
        set("total_tokens", C.compact(row.total_tokens));
        set("split", `${C.compact(row.prompt_tokens)} / ${C.compact(row.completion_tokens)}`);
        set("rank", `#${ranked.indexOf(row) + 1} / ${ranked.length}`);
        if (stats.show_spend) {
          set("spend", C.money(row.spend));
          block.querySelector("[data-llm-spend]").hidden = false;
        }
        block.querySelector('[data-llm="spark"]').replaceChildren(
          C.sparkline(row.daily_tokens, {width: 220, height: 34})
        );
        block.classList.remove("is-idle");
      } else {
        block.classList.add("is-idle");
        set("rank", "Kullanım yok");
      }
      block.hidden = false;
    });

    const fillUsage = (block, row) => {
      if (!row || !row.api_requests) return;
      const label = document.createElement("span");
      label.textContent = `LLM: ${C.compact(row.total_tokens)} token · ${C.compact(row.api_requests)} istek${stats.show_spend ? ` · ${C.money(row.spend)}` : ""}`;
      block.replaceChildren(C.sparkline(row.daily_tokens, {width: 70, height: 18}), label);
      block.hidden = false;
    };
    const byUnit = new Map((stats.units || []).map((row) => [row.key, row]));
    groupBlocks.forEach((block) => fillUsage(block, byGroup.get(block.dataset.genaiGroup)));
    unitBlocks.forEach((block) => fillUsage(block, byUnit.get(block.dataset.genaiUnit)));
  }

  load();
})();
