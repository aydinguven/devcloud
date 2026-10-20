(() => {
  "use strict";

  const esc = value => String(value ?? "").replace(/[&<>'"]/g, character => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[character]);
  const fetchJson = async (url, options = {}) => {
    const response = await fetch(url, {cache: "no-store", ...options});
    const data = response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (!response.ok) {
      let detail = data.detail || `İstek başarısız (${response.status})`;
      if (Array.isArray(detail)) detail = detail.map(item => item.msg).join("; ");
      throw new Error(detail);
    }
    return data;
  };
  const terminalStatuses = new Set(["running", "failed", "stopped"]);
  const releaseTerminalStatuses = new Set(["published", "failed"]);
  const RELEASE_VERSION = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/;
  // Kubernetes publishes the image to the deploy registry (Nexus); availability
  // comes from /api/mlflow/kubernetes/options for the selected model.
  const DEPLOY_TARGETS = {
    "ai-factory": {label: "AI Factory", title: "Modeli Dağıt · AI Factory", submit: "Model Servisini Oluştur", available: true},
    kubernetes: {label: "Kubernetes", title: "Modeli Dağıt · Kubernetes", submit: "Nexus'a Yayınla", available: false},
  };
  const statusBadge = value => value === "running" || value === "published" ? "badge-running" : value === "failed" ? "badge-error" : "badge-neutral";

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
    const name = document.getElementById("mlflow-deploy-name");
    const title = document.getElementById("mlflow-deploy-title");
    const k8s = {
      badge: document.getElementById("mlflow-deploy-k8s-badge"),
      note: document.getElementById("mlflow-deploy-k8s-note"),
      application: document.getElementById("mlflow-deploy-k8s-application"),
      version: document.getElementById("mlflow-deploy-k8s-version"),
      namespace: document.getElementById("mlflow-deploy-k8s-namespace"),
      last: document.getElementById("mlflow-deploy-k8s-last"),
      image: document.getElementById("mlflow-deploy-k8s-image"),
      result: document.getElementById("mlflow-deploy-k8s-result"),
    };
    const targetOptions = Array.from(modal.querySelectorAll("[data-deploy-target-option]"));
    const targetPanels = Array.from(modal.querySelectorAll("[data-deploy-target-panel]"));
    let target = "ai-factory";
    let k8sOptions = null;

    const selectTarget = value => {
      target = DEPLOY_TARGETS[value] ? value : "ai-factory";
      const info = DEPLOY_TARGETS[target];
      targetOptions.forEach(option => {
        const selected = option.dataset.deployTargetOption === target;
        option.setAttribute("aria-checked", String(selected));
        option.classList.toggle("is-selected", selected);
      });
      targetPanels.forEach(panel => { panel.hidden = panel.dataset.deployTargetPanel !== target; });
      // A hidden required field would block the form; only the active target's fields are required.
      flavor.required = target === "ai-factory";
      name.required = target === "ai-factory";
      k8s.application.required = target === "kubernetes";
      k8s.version.required = target === "kubernetes";
      title.textContent = info.title;
      submit.textContent = info.submit;
      submit.disabled = !info.available;
      error.textContent = "";
    };
    targetOptions.forEach(option => option.addEventListener("click", () => selectTarget(option.dataset.deployTargetOption)));
    const events = document.getElementById("mlflow-deploy-events");
    const result = document.getElementById("mlflow-deploy-result");
    const status = document.getElementById("mlflow-deploy-status");
    let flavorsLoaded = false;
    let activeDeployment = null;
    let activeRelease = null;
    let accessToken = "";

    const closeModal = () => {
      modal.classList.remove("open");
      activeDeployment = null;
      activeRelease = null;
    };
    close.addEventListener("click", closeModal);
    cancel.addEventListener("click", closeModal);
    modal.addEventListener("click", event => { if (event.target === modal && !submit.disabled) closeModal(); });

    async function loadFlavors() {
      if (flavorsLoaded) return;
      const items = await fetchJson("/api/workspaces/flavors");
      const available = items.filter(item => item.enabled !== false && item.available !== false);
      flavor.innerHTML = available.map(item => `<option value="${esc(item.id)}">${esc(item.display_name || item.name)} · ${esc(item.cpus)} CPU · ${esc(item.memory_display)}</option>`).join("")
        || '<option value="">Kullanılabilir kaynak profili yok</option>';
      flavorsLoaded = true;
    }

    const selectedApplication = () => (k8sOptions?.applications || []).find(item => item.id === k8s.application.value) || null;
    const renderK8sSummary = () => {
      const application = selectedApplication();
      const version = k8s.version.value.trim();
      k8s.namespace.textContent = application ? application.namespace : "—";
      k8s.last.textContent = application ? (application.last_version || "Henüz yayın yok") : "—";
      k8s.image.textContent = application ? `${application.image_prefix}:${RELEASE_VERSION.test(version) ? version : "X.Y.Z"}` : "—";
    };
    k8s.application.addEventListener("change", () => {
      const application = selectedApplication();
      if (application) k8s.version.value = application.suggested_version;
      renderK8sSummary();
    });
    k8s.version.addEventListener("input", renderK8sSummary);

    async function loadK8sOptions(model) {
      k8sOptions = null;
      DEPLOY_TARGETS.kubernetes.available = false;
      if (target === "kubernetes") submit.disabled = true;
      k8s.badge.textContent = "Kontrol ediliyor";
      k8s.badge.className = "badge badge-neutral";
      k8s.application.innerHTML = '<option value="">Yükleniyor...</option>';
      k8s.version.value = "";
      k8s.note.hidden = true;
      renderK8sSummary();
      try {
        k8sOptions = await fetchJson(`/api/mlflow/kubernetes/options?model_name=${encodeURIComponent(model)}`);
      } catch (value) {
        k8sOptions = {enabled: false, applications: [], message: value.message};
      }
      const applications = k8sOptions.applications || [];
      k8s.application.innerHTML = applications.map(item => `<option value="${esc(item.id)}">${esc(item.repository)} · ${esc(item.project_name)}</option>`).join("")
        || '<option value="">Bu model için uygulama yok</option>';
      if (applications.length) k8s.version.value = applications[0].suggested_version;
      DEPLOY_TARGETS.kubernetes.available = Boolean(k8sOptions.enabled);
      k8s.badge.textContent = k8sOptions.enabled ? "Hazır" : "Kullanılamıyor";
      k8s.badge.className = `badge ${k8sOptions.enabled ? "badge-running" : "badge-neutral"}`;
      k8s.note.textContent = k8sOptions.message || "";
      k8s.note.hidden = !k8sOptions.message;
      renderK8sSummary();
      if (target === "kubernetes") selectTarget("kubernetes");
    }

    const renderEvents = (eventData, buildLog) => {
      const lines = (eventData.events || []).map(item => `[${new Date(item.created_at).toLocaleTimeString("tr-TR")}] ${item.message}`);
      // A failed container build is only explainable from the build log, so
      // append it verbatim under the progress events instead of hiding it.
      if (buildLog) lines.push("", "----- image build günlüğü -----", buildLog);
      events.textContent = lines.join("\n");
      events.scrollTop = events.scrollHeight;
    };

    async function refreshDeployment() {
      if (!activeDeployment) return;
      const [deployment, eventData] = await Promise.all([
        fetchJson(`/api/mlflow/deployments/${encodeURIComponent(activeDeployment)}`),
        fetchJson(`/api/mlflow/deployments/${encodeURIComponent(activeDeployment)}/events`),
      ]);
      status.textContent = deployment.status;
      status.className = `badge ${statusBadge(deployment.status)}`;
      renderEvents(eventData, deployment.build_error_message);
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

    async function refreshRelease() {
      if (!activeRelease) return;
      const releaseId = activeRelease;
      const [release, eventData] = await Promise.all([
        fetchJson(`/api/mlflow/image-releases/${encodeURIComponent(releaseId)}`),
        fetchJson(`/api/mlflow/image-releases/${encodeURIComponent(releaseId)}/events`),
      ]);
      if (activeRelease !== releaseId) return;
      status.textContent = release.status;
      status.className = `badge ${statusBadge(release.status)}`;
      renderEvents(eventData, release.status === "failed" ? release.build_error_message : "");
      if (release.status === "published") {
        k8s.result.hidden = false;
        document.getElementById("mlflow-deploy-k8s-result-image").textContent = release.image_ref;
        document.getElementById("mlflow-deploy-k8s-result-digest").textContent = release.digest;
      }
      if (!releaseTerminalStatuses.has(release.status)) {
        window.setTimeout(() => refreshRelease().catch(value => { error.textContent = value.message; }), 2000);
      } else if (release.status === "failed") {
        error.textContent = release.error_message || "Yayın başarısız oldu.";
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
      k8s.result.hidden = true;
      error.textContent = "";
      activeDeployment = null;
      activeRelease = null;
      accessToken = "";
      selectTarget(button.dataset.deployTarget || "ai-factory");
      document.getElementById("mlflow-deploy-model-name").value = model;
      document.getElementById("mlflow-deploy-model-version").value = version;
      document.getElementById("mlflow-deploy-selection").textContent = `${model} · v${version}`;
      document.getElementById("mlflow-deploy-run").textContent = run ? `Run: ${run}` : "";
      name.value = `${model}-v${version}`.replace(/[^a-zA-Z0-9_-]+/g, "-").slice(0, 60);
      modal.classList.add("open");
      loadFlavors().catch(value => { error.textContent = value.message; });
      loadK8sOptions(model);
    });

    const startProgress = () => {
      form.hidden = true;
      progress.hidden = false;
      events.textContent = "";
    };

    async function submitDeployment() {
      const deployment = await fetchJson("/api/mlflow/deployments", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          name: name.value.trim(),
          model_name: document.getElementById("mlflow-deploy-model-name").value,
          model_version: document.getElementById("mlflow-deploy-model-version").value,
          flavor_id: flavor.value,
          auto_stop_minutes: Number(document.getElementById("mlflow-deploy-auto-stop").value || 0),
          gunicorn_workers: Number(document.getElementById("mlflow-deploy-workers").value || 1),
        }),
      });
      activeDeployment = deployment.id;
      accessToken = deployment.access_token;
      startProgress();
      result.hidden = false;
      document.getElementById("mlflow-deploy-endpoint").textContent = new URL(deployment.endpoint_url, location.origin).href;
      document.getElementById("mlflow-deploy-token").value = accessToken;
      document.getElementById("mlflow-deploy-token-wrap").hidden = false;
      await refreshDeployment();
    }

    async function submitRelease() {
      const version = k8s.version.value.trim();
      if (!RELEASE_VERSION.test(version)) throw new Error("Versiyon X.Y.Z biçiminde olmalıdır (örn. 1.0.0).");
      const release = await fetchJson("/api/mlflow/image-releases", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          application_id: k8s.application.value,
          model_name: document.getElementById("mlflow-deploy-model-name").value,
          model_version: document.getElementById("mlflow-deploy-model-version").value,
          version,
        }),
      });
      activeRelease = release.id;
      startProgress();
      await refreshRelease();
    }

    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (!DEPLOY_TARGETS[target].available) {
        error.textContent = k8sOptions?.message || "Bu hedef şu anda kullanılamıyor.";
        return;
      }
      error.textContent = "";
      submit.disabled = true;
      submit.textContent = "Kuyruğa alınıyor...";
      try {
        if (target === "kubernetes") await submitRelease();
        else await submitDeployment();
      } catch (value) {
        error.textContent = value.message;
        submit.disabled = false;
        submit.textContent = DEPLOY_TARGETS[target].submit;
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
        <td><span class="badge badge-neutral">${esc(DEPLOY_TARGETS[item.target || "ai-factory"]?.label || item.target)}</span></td>
        <td><span class="badge ${statusBadge(item.status)}">${esc(item.status)}</span><br><span class="text-muted">${esc(item.status_message || "")}</span></td>
        <td>${esc(item.flavor_id)}</td>
        <td class="data-value"><code>${esc(item.endpoint_url)}</code></td>
        <td style="white-space:nowrap;">${actions(item)} ${terminalStatuses.has(item.status) ? `<button class="btn btn-danger btn-sm" data-deployment-action="delete" data-deployment-id="${esc(item.id)}">Sil</button>` : ""}</td>
      </tr>`).join("") || '<tr><td colspan="6" class="text-muted">Henüz model deployment oluşturulmadı.</td></tr>';
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

  function initReleaseList() {
    const root = document.getElementById("mlflow-release-list");
    if (!root) return;
    const status = document.getElementById("mlflow-release-list-status");
    const body = document.getElementById("mlflow-release-list-body");
    const imageCell = item => item.status === "published"
      ? `<code>${esc(item.image_ref)}</code><br><span class="text-muted" title="${esc(item.digest)}">${esc(item.digest.slice(0, 19))}…</span>`
      : `<code>${esc(item.image_ref || `${item.rfs_code}/${item.app_name}:${item.version}`)}</code>`;
    async function load() {
      const data = await fetchJson("/api/mlflow/image-releases");
      body.innerHTML = (data.releases || []).map(item => `<tr>
        <td><strong>${esc(item.rfs_code)}/${esc(item.app_name)}</strong><br><span class="text-muted">${esc(item.model_name)} v${esc(item.model_version)}</span></td>
        <td><span class="badge badge-neutral">Kubernetes</span><br><span class="text-muted">${esc(item.namespace)}</span></td>
        <td><strong>${esc(item.version)}</strong></td>
        <td><span class="badge ${statusBadge(item.status)}">${esc(item.status)}</span><br><span class="text-muted" title="${esc(item.error_message || "")}">${esc(item.status_message || "")}</span></td>
        <td class="data-value">${imageCell(item)}</td>
        <td>${esc(item.username)}<br><span class="text-muted">${esc(new Date(item.created_at).toLocaleString("tr-TR"))}</span></td>
        <td style="white-space:nowrap;">${item.status === "failed" ? `<button class="btn btn-primary btn-sm" data-release-retry="${esc(item.id)}">Yeniden Dene</button>` : ""}</td>
      </tr>`).join("") || '<tr><td colspan="7" class="text-muted">Henüz Kubernetes yayını yapılmadı.</td></tr>';
      status.textContent = "";
      status.className = "";
    }
    root.addEventListener("click", async event => {
      const button = event.target.closest("[data-release-retry]");
      if (!button) return;
      button.disabled = true;
      try {
        await fetchJson(`/api/mlflow/image-releases/${encodeURIComponent(button.dataset.releaseRetry)}/retry`, {method: "POST"});
        await load();
      } catch (value) {
        status.textContent = value.message;
        status.className = "text-destructive";
        button.disabled = false;
      }
    });
    load().catch(value => { status.textContent = value.message; status.className = "text-destructive"; });
    window.setInterval(() => load().catch(() => {}), 5000);
  }

  initDeployModal();
  initDeploymentList();
  initReleaseList();
})();
