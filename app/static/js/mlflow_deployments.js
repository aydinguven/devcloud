(() => {
  "use strict";

  const esc = value => String(value ?? "").replace(/[&<>'"]/g, character => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[character]);
  const fetchJson = async (url, options = {}) => {
    const response = await fetch(url, {cache: "no-store", ...options});
    const data = response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `İstek başarısız (${response.status})`);
    return data;
  };
  const terminalStatuses = new Set(["running", "failed", "stopped"]);
  const statusBadge = value => value === "running" ? "badge-running" : value === "failed" ? "badge-error" : "badge-neutral";

  function initDeployModal() {
    const modal = document.getElementById("mlflow-deploy-modal");
    if (!modal) return;
    const form = document.getElementById("mlflow-deploy-form");
    const progress = document.getElementById("mlflow-deploy-progress");
    const close = document.getElementById("mlflow-deploy-close");
    const cancel = document.getElementById("mlflow-deploy-cancel");
    const submit = document.getElementById("mlflow-deploy-submit");
    const error = document.getElementById("mlflow-deploy-error");
    const flavor = document.getElementById("mlflow-deploy-flavor");
    const events = document.getElementById("mlflow-deploy-events");
    const result = document.getElementById("mlflow-deploy-result");
    const status = document.getElementById("mlflow-deploy-status");
    let flavorsLoaded = false;
    let activeDeployment = null;
    let accessToken = "";

    const closeModal = () => {
      modal.classList.remove("open");
      activeDeployment = null;
    };
    close.addEventListener("click", closeModal);
    cancel.addEventListener("click", closeModal);
    modal.addEventListener("click", event => { if (event.target === modal && !submit.disabled) closeModal(); });

    async function loadFlavors() {
      if (flavorsLoaded) return;
      const items = await fetchJson("/api/workspaces/flavors");
      const available = items.filter(item => item.enabled !== false && item.available !== false);
      flavor.innerHTML = available.map(item => `<option value="${esc(item.id)}">${esc(item.display_name || item.name)} · ${esc(item.cpus)} CPU · ${esc(item.memory_display)}</option>`).join("");
      if (!available.length) flavor.innerHTML = '<option value="">Kullanılabilir kaynak profili yok</option>';
      flavorsLoaded = true;
    }

    async function refreshDeployment() {
      if (!activeDeployment) return;
      const [deployment, eventData] = await Promise.all([
        fetchJson(`/api/mlflow/deployments/${encodeURIComponent(activeDeployment)}`),
        fetchJson(`/api/mlflow/deployments/${encodeURIComponent(activeDeployment)}/events`),
      ]);
      status.textContent = deployment.status;
      status.className = `badge ${statusBadge(deployment.status)}`;
      const lines = (eventData.events || []).map(item => `[${new Date(item.created_at).toLocaleTimeString("tr-TR")}] ${item.message}`);
      // A failed container build is only explainable from the build log, so
      // append it verbatim under the progress events instead of hiding it.
      if (deployment.build_error_message) {
        lines.push("", "----- image build günlüğü -----", deployment.build_error_message);
      }
      events.textContent = lines.join("\n");
      events.scrollTop = events.scrollHeight;
      if (deployment.status === "running") {
        result.hidden = false;
        document.getElementById("mlflow-deploy-endpoint").textContent = new URL(deployment.endpoint_url, location.origin).href;
        document.getElementById("mlflow-deploy-token").value = accessToken;
        document.getElementById("mlflow-deploy-token-wrap").hidden = !accessToken;
      }
      if (!terminalStatuses.has(deployment.status)) {
        window.setTimeout(() => refreshDeployment().catch(value => { error.textContent = value.message; }), 2000);
      } else if (deployment.status === "failed") {
        error.textContent = deployment.error_message || "Deployment başarısız oldu.";
      }
    }

    document.addEventListener("click", event => {
      const button = event.target.closest("[data-deploy-model]");
      if (!button) return;
      const model = button.dataset.modelName || "";
      const version = button.dataset.modelVersion || "";
      const run = button.dataset.runId || "";
      form.hidden = false;
      progress.hidden = true;
      result.hidden = true;
      error.textContent = "";
      submit.disabled = false;
      activeDeployment = null;
      accessToken = "";
      document.getElementById("mlflow-deploy-model-name").value = model;
      document.getElementById("mlflow-deploy-model-version").value = version;
      document.getElementById("mlflow-deploy-selection").textContent = `${model} · v${version}`;
      document.getElementById("mlflow-deploy-run").textContent = run ? `Run: ${run}` : "";
      document.getElementById("mlflow-deploy-name").value = `${model}-v${version}`.replace(/[^a-zA-Z0-9_-]+/g, "-").slice(0, 60);
      modal.classList.add("open");
      loadFlavors().catch(value => { error.textContent = value.message; });
    });

    form.addEventListener("submit", async event => {
      event.preventDefault();
      error.textContent = "";
      submit.disabled = true;
      submit.textContent = "Kuyruğa alınıyor...";
      try {
        const deployment = await fetchJson("/api/mlflow/deployments", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            name: document.getElementById("mlflow-deploy-name").value.trim(),
            model_name: document.getElementById("mlflow-deploy-model-name").value,
            model_version: document.getElementById("mlflow-deploy-model-version").value,
            flavor_id: flavor.value,
            auto_stop_minutes: Number(document.getElementById("mlflow-deploy-auto-stop").value || 0),
            gunicorn_workers: Number(document.getElementById("mlflow-deploy-workers").value || 1),
          }),
        });
        activeDeployment = deployment.id;
        accessToken = deployment.access_token;
        form.hidden = true;
        progress.hidden = false;
        result.hidden = false;
        document.getElementById("mlflow-deploy-endpoint").textContent = new URL(deployment.endpoint_url, location.origin).href;
        document.getElementById("mlflow-deploy-token").value = accessToken;
        document.getElementById("mlflow-deploy-token-wrap").hidden = false;
        await refreshDeployment();
      } catch (value) {
        error.textContent = value.message;
        submit.disabled = false;
        submit.textContent = "Model Servisini Oluştur";
      }
    });
  }

  function initDeploymentList() {
    const root = document.getElementById("mlflow-deployment-list");
    if (!root) return;
    const status = document.getElementById("mlflow-deployment-list-status");
    const body = document.getElementById("mlflow-deployment-list-body");
    const actions = deployment => {
      let lifecycle = "";
      if (deployment.status === "running") lifecycle = `<button class="btn btn-secondary btn-sm" data-deployment-action="stop" data-deployment-id="${esc(deployment.id)}">Durdur</button>`;
      if (deployment.status === "stopped") lifecycle = `<button class="btn btn-primary btn-sm" data-deployment-action="start" data-deployment-id="${esc(deployment.id)}">Başlat</button>`;
      if (deployment.status === "failed") lifecycle = `<button class="btn btn-primary btn-sm" data-deployment-action="retry" data-deployment-id="${esc(deployment.id)}">Yeniden Dene</button>`;
      return `${lifecycle} <button class="btn btn-secondary btn-sm" data-deployment-action="token" data-deployment-id="${esc(deployment.id)}">Token Yenile</button>`;
    };
    async function load() {
      const data = await fetchJson("/api/mlflow/deployments");
      body.innerHTML = (data.deployments || []).map(item => `<tr>
        <td><strong>${esc(item.name)}</strong><br><span class="text-muted">${esc(item.model_name)} v${esc(item.model_version)}</span></td>
        <td><span class="badge ${statusBadge(item.status)}">${esc(item.status)}</span><br><span class="text-muted">${esc(item.status_message || "")}</span></td>
        <td>${esc(item.flavor_id)}</td>
        <td class="data-value"><code>${esc(item.endpoint_url)}</code></td>
        <td style="white-space:nowrap;">${actions(item)} ${terminalStatuses.has(item.status) ? `<button class="btn btn-danger btn-sm" data-deployment-action="delete" data-deployment-id="${esc(item.id)}">Sil</button>` : ""}</td>
      </tr>`).join("") || '<tr><td colspan="5" class="text-muted">Henüz model deployment oluşturulmadı.</td></tr>';
      status.textContent = "";
    }
    root.addEventListener("click", async event => {
      const button = event.target.closest("[data-deployment-action]");
      if (!button) return;
      const action = button.dataset.deploymentAction;
      if (action === "delete" && !confirm("Model deployment ve servis workspace silinsin mi?")) return;
      button.disabled = true;
      try {
        const actionResult = await fetchJson(`/api/mlflow/deployments/${encodeURIComponent(button.dataset.deploymentId)}${action === "delete" ? "" : `/${action}`}`, {method: action === "delete" ? "DELETE" : "POST"});
        if (action === "token" && actionResult.access_token) {
          window.prompt("Yeni erişim tokenı — şimdi güvenli bir yere kopyalayın:", actionResult.access_token);
        }
        await load();
      } catch (value) {
        status.textContent = value.message;
        status.className = "text-destructive";
      } finally {
        button.disabled = false;
      }
    });
    load().catch(value => { status.textContent = value.message; status.className = "text-destructive"; });
    window.setInterval(() => load().catch(() => {}), 5000);
  }

  initDeployModal();
  initDeploymentList();
})();
