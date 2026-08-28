// ==============================================================================
// DevCloud Client Application Logic
// ==============================================================================

document.addEventListener("DOMContentLoaded", () => {
  initWorkspaceCreationModal();
  initActionButtons();
  initLogPolling();
  initQuotaForms();
  initDirectorySettings();
  initNodeManagement();
  initMlflowSettings();
  initDownloadSettings();
  initHttpsSettings();
  initDownloadUpdater();
  initLiveMetricsPolling();
  initWorkspaceTabs();
  initPortExposer();
  initSnapshotModal();
  initAdminPlatformUpdater();
  initAdminFilters();
  initWorkspaceImageManager();
});

function initAdminFilters() {
  document.querySelectorAll("[data-admin-filter]").forEach((input) => {
    const selector = input.dataset.adminFilter;
    if (!selector) return;
    input.addEventListener("input", () => {
      const query = input.value.trim().toLocaleLowerCase("tr-TR");
      document.querySelectorAll(selector).forEach((item) => {
        const haystack = item.textContent.toLocaleLowerCase("tr-TR");
        item.hidden = Boolean(query) && !haystack.includes(query);
      });
    });
  });
}

function initWorkspaceImageManager() {
  const table = document.getElementById("workspace-image-table");
  if (!table) return;
  const tbody = table.querySelector("tbody");
  const count = document.getElementById("workspace-image-count");
  const globalStatus = document.getElementById("workspace-image-global-status");
  const registryForm = document.getElementById("workspace-image-registry-form");
  const uploadForm = document.getElementById("workspace-image-upload-form");

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
  const humanSize = (bytes) => {
    let value = Number(bytes || 0);
    const units = ["B", "KB", "MB", "GB", "TB"];
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
  };
  const setStatus = (element, message, error = false) => {
    if (!element) return;
    element.textContent = message;
    element.className = `quota-form-status ${error ? "quota-status-error" : "quota-status-success"}`;
  };

  async function loadImages() {
    try {
      const response = await fetch("/api/admin/workspace-images", { cache: "no-store" });
      const images = await response.json().catch(() => []);
      if (!response.ok) throw new Error(images.detail || `Katalog okunamadı (${response.status})`);
      count.textContent = `${images.length} sürüm`;
      tbody.innerHTML = images.length ? images.map((image) => `
        <tr>
          <td><strong>${escapeHtml(image.display_name)}</strong><br><code>${escapeHtml(image.template_id)}</code><br><small>${escapeHtml(image.image_ref)}</small></td>
          <td><span class="badge badge-neutral">${escapeHtml(image.source_type)}</span><br><small>${escapeHtml(image.source_ref)}</small></td>
          <td><code title="${escapeHtml(image.digest)}">${escapeHtml((image.digest || "-").slice(0, 24))}</code><br><code title="${escapeHtml(image.sha256)}">${escapeHtml(image.sha256.slice(0, 24))}…</code></td>
          <td>${humanSize(image.size)}</td>
          <td><strong>${image.synced_workers} / ${image.total_workers}</strong></td>
          <td><span class="badge ${image.enabled ? "badge-running" : "badge-neutral"}">${image.enabled ? "Etkin" : "Pasif"}</span></td>
          <td><div style="display:flex;gap:.4rem;flex-wrap:wrap;">
            <button class="btn btn-secondary btn-sm" data-image-toggle="${escapeHtml(image.id)}" data-enabled="${image.enabled}">${image.enabled ? "Devre Dışı" : "Etkinleştir"}</button>
            <button class="btn btn-danger btn-sm" data-image-delete="${escapeHtml(image.id)}">Sil</button>
          </div></td>
        </tr>`).join("") : '<tr><td colspan="7">Henüz workspace image eklenmedi.</td></tr>';
    } catch (error) {
      setStatus(globalStatus, error.message, true);
    }
  }

  async function submitForm(form, url, json = false) {
    const button = form.querySelector('button[type="submit"]');
    const status = form.querySelector("[data-image-form-status]");
    button.disabled = true;
    status.textContent = "İçe aktarılıyor; büyük image'lar birkaç dakika sürebilir...";
    status.className = "quota-form-status";
    try {
      let body;
      const options = { method: "POST" };
      if (json) {
        const values = Object.fromEntries(new FormData(form).entries());
        body = JSON.stringify(values);
        options.headers = { "Content-Type": "application/json" };
      } else {
        body = new FormData(form);
      }
      options.body = body;
      const response = await fetch(url, options);
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || `Image içe aktarılamadı (${response.status})`);
      setStatus(status, "Image doğrulandı, etkinleştirildi ve worker kataloğuna eklendi.");
      form.reset();
      await loadImages();
    } catch (error) {
      setStatus(status, error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  registryForm.addEventListener("submit", (event) => {
    event.preventDefault();
    submitForm(registryForm, "/api/admin/workspace-images/import", true);
  });
  uploadForm.addEventListener("submit", (event) => {
    event.preventDefault();
    submitForm(uploadForm, "/api/admin/workspace-images/upload");
  });
  document.getElementById("btn-refresh-workspace-images").addEventListener("click", loadImages);
  tbody.addEventListener("click", async (event) => {
    const toggle = event.target.closest("[data-image-toggle]");
    const remove = event.target.closest("[data-image-delete]");
    if (!toggle && !remove) return;
    const imageId = (toggle || remove).dataset.imageToggle || (toggle || remove).dataset.imageDelete;
    if (remove && !window.confirm("Bu image arşivi controller'dan kalıcı olarak silinsin mi?")) return;
    try {
      const response = await fetch(`/api/admin/workspace-images/${encodeURIComponent(imageId)}`, {
        method: remove ? "DELETE" : "PATCH",
        headers: remove ? {} : { "Content-Type": "application/json" },
        body: remove ? undefined : JSON.stringify({ enabled: toggle.dataset.enabled !== "true" }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || `İşlem başarısız (${response.status})`);
      await loadImages();
    } catch (error) {
      setStatus(globalStatus, error.message, true);
    }
  });
  loadImages();
  window.setInterval(loadImages, 20000);
}

// 1. Workspace Creation Modal with Live Real-Time Deployment Log Streamer
function initWorkspaceCreationModal() {
  const modalBackdrop = document.getElementById("create-modal");
  const openBtn = document.getElementById("btn-open-create-modal");
  const closeBtn = document.getElementById("btn-close-create-modal");
  const form = document.getElementById("create-workspace-form");
  const terminalBox = document.getElementById("deploy-terminal-box");
  const terminal = document.getElementById("deploy-terminal");
  const statusBadge = document.getElementById("deploy-status-badge");
  const modalFooter = document.getElementById("modal-footer-actions");
  const submitBtn = document.getElementById("btn-submit-deploy");
  const cancelBtn = document.getElementById("btn-cancel-deploy");

  if (!modalBackdrop) return;

  if (openBtn) {
    openBtn.addEventListener("click", () => {
      if (terminalBox) terminalBox.style.display = "none";
      if (terminal) terminal.innerHTML = "";
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.style.display = "inline-flex";
        submitBtn.innerHTML = "Çalışma Alanını Kur";
      }
      if (cancelBtn) cancelBtn.style.display = "inline-flex";
      modalBackdrop.classList.add("open");
    });
  }

  if (closeBtn) {
    closeBtn.addEventListener("click", () => {
      modalBackdrop.classList.remove("open");
    });
  }

  modalBackdrop.addEventListener("click", (e) => {
    if (e.target === modalBackdrop && (!submitBtn || !submitBtn.disabled)) {
      modalBackdrop.classList.remove("open");
    }
  });

  // Template Selection
  const templateCards = document.querySelectorAll(".template-card");
  const templateInput = document.getElementById("input-template-id");
  templateCards.forEach((card) => {
    card.addEventListener("click", () => {
      templateCards.forEach((c) => c.classList.remove("selected"));
      card.classList.add("selected");
      if (templateInput) {
        templateInput.value = card.dataset.templateId;
      }
    });
  });

  // Flavor Selection
  const flavorCards = document.querySelectorAll(".flavor-card");
  const flavorInput = document.getElementById("input-flavor-id");
  flavorCards.forEach((card) => {
    card.addEventListener("click", () => {
      flavorCards.forEach((c) => c.classList.remove("selected"));
      card.classList.add("selected");
      if (flavorInput) {
        flavorInput.value = card.dataset.flavorId;
      }
    });
  });

  function appendLog(text, level = "info") {
    if (!terminal) return;
    const now = new Date().toTimeString().split(" ")[0];
    const line = document.createElement("div");
    line.className = `log-line log-${level}`;
    line.textContent = `[${now}] ${text}`;
    terminal.appendChild(line);
    terminal.scrollTop = terminal.scrollHeight;
  }

  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();

      const name = document.getElementById("input-workspace-name").value.trim();
      const description = document.getElementById("input-workspace-desc")?.value.trim() || "";
      const templateId = templateInput.value;
      const flavorId = flavorInput.value;
      const autoStopInput = document.getElementById("input-auto-stop");
      const autoStopMinutes = autoStopInput ? parseInt(autoStopInput.value, 10) || 0 : 0;

      if (!name) return;

      if (terminalBox) terminalBox.style.display = "block";
      if (terminal) terminal.innerHTML = "";
      if (statusBadge) {
        statusBadge.className = "badge badge-creating";
        statusBadge.textContent = "Kuruluyor...";
      }

      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="pulse-dot" style="margin-right: 0.35rem;"></span> Kuruluyor...';
      }
      if (cancelBtn) cancelBtn.style.display = "none";

      appendLog(`'${name}' için kurulum süreci başlatılıyor (${templateId}, ${flavorId})...`, "dim");

      try {
        const response = await fetch("/api/workspaces/deploy-stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: name,
            description: description,
            template_id: templateId,
            flavor_id: flavorId,
            auto_stop_minutes: autoStopMinutes,
          }),
        });

        if (!response.ok) {
          const err = await response.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP Hatası: ${response.status}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let deploymentCompleted = false;

        function processLine(rawLine) {
          const line = rawLine.trim();
          if (!line || !line.startsWith("data: ")) return;
          try {
            const data = JSON.parse(line.slice(6));
            if (data.type === "log") {
              appendLog(data.text, data.level || "info");
            } else if (data.type === "error") {
              appendLog(data.text || data.error, "error");
              if (statusBadge) {
                statusBadge.className = "badge badge-error";
                statusBadge.textContent = "Hata";
              }
              if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.innerHTML = "Kurulumu Yeniden Dene";
              }
              if (cancelBtn) cancelBtn.style.display = "inline-flex";
            } else if (data.type === "done") {
              deploymentCompleted = true;
              if (statusBadge) {
                statusBadge.className = "badge badge-running";
                statusBadge.textContent = "Aktif";
              }
              appendLog("Container çevrimiçi ve doğrulandı. Çalışma alanı hazır.", "success");

              if (modalFooter) {
                modalFooter.innerHTML = `
                  <button type="button" class="btn btn-secondary" onclick="window.location.reload();">Panele Dön</button>
                  <a href="${data.web_url}" target="_blank" class="btn btn-success" style="padding: 0.65rem 1.5rem; font-weight: 600;">
                    <span>↗</span> IDE'yi Şimdi Aç
                  </a>
                `;
              }

              setTimeout(() => {
                window.location.reload();
              }, 2000);
            }
          } catch (parseErr) {
            console.warn("SSE parse error:", line);
          }
        }

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop();

          for (const line of lines) {
            processLine(line);
          }
        }

        if (buffer) {
          processLine(buffer);
        }

        if (!deploymentCompleted && statusBadge && statusBadge.textContent !== "Hata") {
          setTimeout(() => {
            window.location.reload();
          }, 1000);
        }
      } catch (err) {
        appendLog(`Ağ bağlantısı hatası: ${err.message}`, "error");
        if (statusBadge) {
          statusBadge.className = "badge badge-error";
          statusBadge.textContent = "Ağ Hatası";
        }
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.innerHTML = "Kurulumu Yeniden Dene";
        }
        if (cancelBtn) cancelBtn.style.display = "inline-flex";
      }
    });
  }
}

// 2. Action Buttons (Start, Stop, Delete)
function initActionButtons() {
  document.querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      const action = btn.dataset.action;
      const workspaceId = btn.dataset.workspaceId;

      if (action === "delete") {
        if (!confirm("Bu çalışma alanını kalıcı olarak silmek istediğinizden emin misiniz? Container ve kalıcı depolamadaki tüm dosyalar silinecektir.")) {
          return;
        }
      }

      btn.disabled = true;
      const originalHtml = btn.innerHTML;
      btn.innerHTML = "...";

      try {
        let url = `/api/workspaces/${workspaceId}`;
        let method = "GET";

        if (action === "start") {
          url += "/start";
          method = "POST";
        } else if (action === "stop") {
          url += "/stop";
          method = "POST";
        } else if (action === "delete") {
          method = "DELETE";
        }

        const res = await fetch(url, { method: method });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          alert(err.detail || `${action} işlemi başarısız.`);
          btn.disabled = false;
          btn.innerHTML = originalHtml;
          return;
        }

        if (action === "delete" && window.location.pathname.startsWith("/workspaces/")) {
          window.location.href = "/";
          return;
        }

        window.location.reload();
      } catch (err) {
        alert("İşlem hatası: " + err.message);
        btn.disabled = false;
        btn.innerHTML = originalHtml;
      }
    });
  });
}

// 3. Live Log Poller for Workspace Detail page
function initLogPolling() {
  const logTerminal = document.getElementById("workspace-logs-output");
  if (!logTerminal) return;

  const workspaceId = logTerminal.dataset.workspaceId;
  if (!workspaceId) return;

  async function fetchLogs() {
    try {
      const res = await fetch(`/api/workspaces/${workspaceId}/logs?tail=150`);
      if (res.ok) {
        const data = await res.json();
        logTerminal.textContent = data.logs || "Henüz log yok.";
      }
    } catch (e) {
      console.warn("Container logları alınamadı:", e);
    }
  }

  fetchLogs();
  setInterval(fetchLogs, 3000);
}

// 4. Admin per-user quota editor
function initQuotaForms() {
  document.querySelectorAll(".quota-form").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const userId = form.dataset.userId;
      const submitButton = form.querySelector('button[type="submit"]');
      const status = form.querySelector(".quota-form-status");
      const cpuQuota = Number(form.elements.cpu_quota.value);
      const memoryGbQuota = Number(form.elements.memory_gb_quota.value);
      const diskGbQuota = Number(form.elements.disk_gb_quota.value);

      if (![cpuQuota, memoryGbQuota, diskGbQuota].every(Number.isFinite)) {
        status.textContent = "Geçerli sayılar girin.";
        status.className = "quota-form-status quota-status-error";
        return;
      }

      submitButton.disabled = true;
      status.textContent = "Kaydediliyor...";
      status.className = "quota-form-status";
      try {
        const response = await fetch(`/api/admin/users/${userId}/quota`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            cpu_quota: cpuQuota,
            memory_mb_quota: Math.round(memoryGbQuota * 1024),
            disk_mb_quota: Math.round(diskGbQuota * 1024),
          }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data.detail || `Kota güncellenemedi (${response.status})`);
        }
        status.textContent = "Kaydedildi";
        status.className = "quota-form-status quota-status-success";
        setTimeout(() => window.location.reload(), 650);
      } catch (error) {
        status.textContent = error.message;
        status.className = "quota-form-status quota-status-error";
        submitButton.disabled = false;
      }
    });
  });
}

// 5. Admin LDAP / Active Directory settings
function initDirectorySettings() {
  const form = document.getElementById("directory-settings-form");
  if (!form) return;

  const testButton = document.getElementById("btn-test-directory");
  const saveButton = document.getElementById("btn-save-directory");
  const status = document.getElementById("directory-form-status");
  const badge = document.getElementById("directory-status-badge");

  function payload() {
    const data = new FormData(form);
    const password = String(data.get("bind_password") || "");
    return {
      enabled: form.elements.enabled.checked,
      server_host: String(data.get("server_host") || "").trim(),
      server_port: Number(data.get("server_port")),
      use_ssl: form.elements.use_ssl.checked,
      validate_tls: form.elements.validate_tls.checked,
      ca_cert_file: String(data.get("ca_cert_file") || "").trim(),
      connect_timeout_seconds: Number(data.get("connect_timeout_seconds")),
      bind_dn: String(data.get("bind_dn") || "").trim(),
      bind_password: password || null,
      user_base_dn: String(data.get("user_base_dn") || "").trim(),
      user_filter: String(data.get("user_filter") || "").trim(),
      username_attribute: String(data.get("username_attribute") || "").trim(),
      email_attribute: String(data.get("email_attribute") || "").trim(),
      display_name_attribute: String(data.get("display_name_attribute") || "").trim(),
      group_membership_attribute: String(data.get("group_membership_attribute") || "").trim(),
      required_group_dn: String(data.get("required_group_dn") || "").trim(),
      admin_group_dn: String(data.get("admin_group_dn") || "").trim(),
      nested_group_search: form.elements.nested_group_search.checked,
    };
  }

  function setStatus(message, isError = false) {
    status.textContent = message;
    status.className = `quota-form-status ${isError ? "quota-status-error" : "quota-status-success"}`;
  }

  async function send(url, method) {
    const response = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload()),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      let detail = data.detail || `İşlem başarısız (${response.status})`;
      if (Array.isArray(detail)) detail = detail.map((item) => item.msg).join("; ");
      throw new Error(detail);
    }
    return data;
  }

  testButton?.addEventListener("click", async () => {
    testButton.disabled = true;
    saveButton.disabled = true;
    status.textContent = "LDAPS bağlantısı test ediliyor...";
    status.className = "quota-form-status";
    try {
      const result = await send("/api/admin/directory-settings/test", "POST");
      setStatus(`${result.message} ${result.server} · ${result.response_time_ms} ms`);
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      testButton.disabled = false;
      saveButton.disabled = false;
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    testButton.disabled = true;
    saveButton.disabled = true;
    status.textContent = "Kaydediliyor...";
    status.className = "quota-form-status";
    try {
      const result = await send("/api/admin/directory-settings", "PUT");
      form.elements.bind_password.value = "";
      form.elements.bind_password.placeholder = "Kayıtlı parolayı korumak için boş bırakın";
      badge.className = `badge ${result.enabled ? "badge-running" : "badge-stopped"}`;
      badge.textContent = result.enabled ? "Etkin" : "Devre Dışı";
      setStatus("LDAP ayarları kaydedildi.");
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      testButton.disabled = false;
      saveButton.disabled = false;
    }
  });
}

function initNodeManagement() {
  const form = document.getElementById("node-create-form");
  const tokenBox = document.getElementById("node-enrollment-token");
  const status = document.getElementById("node-create-status");

  function renderWorkerTokenBox(data) {
    if (!tokenBox) return;
    const controllerUrl = window.location.origin;
    const singleLineCmd = `curl -fsSL '${controllerUrl}/download/install-worker.sh' | sudo DEVCLOUD_NODE_ID='${data.id}' bash`;

    tokenBox.style.display = "block";
    tokenBox.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.75rem;border-bottom:1px solid rgba(255,255,255,0.1);padding-bottom:0.5rem;">
        <strong style="color:var(--text-main,#fff);font-size:0.95rem;">✨ Worker Token / Bağlantı Bilgisi: ${data.name}</strong>
        <span style="font-size:0.75rem;color:var(--text-muted,#9ca3af);">Token yalnızca şimdi gösterilir</span>
      </div>
      <div style="margin-bottom:0.75rem;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.35rem;">
          <span style="font-size:0.85rem;font-weight:600;color:var(--accent,#38bdf8);">🚀 Tek Satırda Kurulum ve Bağlantı Komutu:</span>
          <button type="button" class="btn btn-secondary btn-sm" id="btn-copy-worker-cmd" style="padding:0.25rem 0.65rem;font-size:0.75rem;">Kopyala</button>
        </div>
        <pre id="worker-single-line-cmd" style="background:rgba(0,0,0,0.4);padding:0.6rem;border-radius:4px;overflow-x:auto;margin:0;font-size:0.8rem;white-space:pre-wrap;word-break:break-all;color:#a7f3d0;">${singleLineCmd}</pre>
      </div>
      <div style="font-size:0.8rem;color:var(--text-muted,#9ca3af);margin-top:0.75rem;line-height:1.5;">
        <div><strong>Node ID:</strong> <code style="color:#e2e8f0;">${data.id}</code></div>
        <div><strong>Token:</strong> <code style="color:#e2e8f0;">${data.enrollment_token}</code></div>
        <div style="margin-top:0.5rem;font-size:0.75rem;">
          <strong>Manuel Yapılandırma (/etc/devcloud/worker.env):</strong>
          <pre style="background:rgba(0,0,0,0.25);padding:0.5rem;border-radius:4px;margin-top:0.25rem;font-size:0.75rem;color:#e2e8f0;white-space:pre;">DEVCLOUD_CONTROLLER_URL=${controllerUrl}
DEVCLOUD_NODE_ID=${data.id}
DEVCLOUD_NODE_TOKEN=${data.enrollment_token}</pre>
        </div>
      </div>
    `;

    const copyBtn = document.getElementById("btn-copy-worker-cmd");
    if (copyBtn) {
      copyBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(singleLineCmd);
          copyBtn.textContent = "Kopyalandı! ✓";
          setTimeout(() => { copyBtn.textContent = "Kopyala"; }, 2000);
        } catch (e) {
          const textarea = document.createElement("textarea");
          textarea.value = singleLineCmd;
          document.body.appendChild(textarea);
          textarea.select();
          document.execCommand("copy");
          document.body.removeChild(textarea);
          copyBtn.textContent = "Kopyalandı! ✓";
          setTimeout(() => { copyBtn.textContent = "Kopyala"; }, 2000);
        }
      });
    }
  }

  // Real-time SSE Stream for Nodes Telemetry and Status
  const nodesTable = document.getElementById("admin-nodes-table");
  if (nodesTable) {
    try {
      const eventSource = new EventSource("/api/admin/nodes/events-stream");
      eventSource.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "node.connected" || msg.type === "node.disconnected") {
            const row = document.querySelector(`tr[data-node-id="${msg.data.node_id}"]`);
            if (row) {
              const badge = row.querySelector(".node-status-badge");
              if (badge) {
                const isOnline = msg.data.status === "online";
                badge.className = `badge ${isOnline ? "badge-running" : "badge-stopped"} node-status-badge`;
                badge.textContent = (msg.data.status || "offline").toUpperCase();
              }
            }
          } else if (msg.type === "node.telemetry") {
            const d = msg.data;
            const row = document.querySelector(`tr[data-node-id="${d.node_id}"]`);
            if (row) {
              const badge = row.querySelector(".node-status-badge");
              if (badge) {
                const isOnline = d.status === "online";
                badge.className = `badge ${isOnline ? "badge-running" : "badge-stopped"} node-status-badge`;
                badge.textContent = (d.status || "offline").toUpperCase();
              }
              const cpuText = row.querySelector(".node-cpu-text");
              const cpuBar = row.querySelector(".node-cpu-bar");
              if (cpuText) cpuText.innerHTML = `${d.cpu_percent}% <span style="font-size: 0.68rem; color: var(--text-muted); font-weight: 400;">(${d.cpu_total}c)</span>`;
              if (cpuBar) cpuBar.style.width = `${Math.min(d.cpu_percent, 100)}%`;

              const ramText = row.querySelector(".node-ram-text");
              const ramBar = row.querySelector(".node-ram-bar");
              const usedGb = (d.memory_used_mb / 1024).toFixed(1);
              const totalGb = (d.memory_total_mb / 1024).toFixed(1);
              const ramPct = d.memory_total_mb ? Math.min((d.memory_used_mb / d.memory_total_mb) * 100, 100) : 0;
              if (ramText) ramText.innerHTML = `${usedGb}<span style="font-size: 0.68rem; color: var(--text-muted); font-weight: 400;">/${totalGb}G</span>`;
              if (ramBar) ramBar.style.width = `${ramPct}%`;

              const diskCell = row.querySelector(".node-disk-cell span");
              if (diskCell) {
                const dUsedGb = (d.disk_used_mb / 1024).toFixed(1);
                diskCell.textContent = `${dUsedGb} GB`;
              }

              const cntBadge = row.querySelector(".node-container-count");
              if (cntBadge) cntBadge.textContent = `${d.active_containers_count} cnt`;

              const lastSeenCell = row.querySelector(".node-last-seen-cell");
              if (lastSeenCell && d.last_seen_at) {
                lastSeenCell.textContent = d.last_seen_at.replace("T", " ").substring(0, 19);
              }
            }
          }
        } catch (_) {}
      };
    } catch (_) {}
  }

  if (form) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector("button[type=submit]");
      button.disabled = true;
      status.textContent = "Worker kaydediliyor...";
      try {
        const response = await fetch("/api/admin/nodes", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: form.elements.name.value.trim(), schedulable: true, labels: {}}),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `Worker eklenemedi (${response.status})`);

        renderWorkerTokenBox(data);
        status.textContent = "Worker kaydedildi. Yukarıdaki tek satırlık komutu kopyalayıp worker makinesinde çalıştırın.";
        status.className = "quota-form-status quota-status-success";
        form.reset();
      } catch (error) {
        status.textContent = error.message;
        status.className = "quota-form-status quota-status-error";
      } finally {
        button.disabled = false;
      }
    });
  }

  document.querySelectorAll(".node-toggle-schedule").forEach(button => {
    button.addEventListener("click", async () => {
      const row = button.closest("tr");
      const schedulable = button.dataset.schedulable === "true";
      button.disabled = true;
      try {
        const response = await fetch(`/api/admin/nodes/${row.dataset.nodeId}`, {
          method: "PATCH",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({schedulable: !schedulable}),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Worker güncellenemedi.");
        window.location.reload();
      } catch (error) {
        if (status) {
          status.textContent = error.message;
          status.className = "quota-form-status quota-status-error";
        }
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll(".node-rotate-token-btn").forEach(button => {
    button.addEventListener("click", async () => {
      const row = button.closest("tr");
      const nodeId = button.dataset.nodeId || row.dataset.nodeId;
      const nodeName = button.dataset.nodeName || "worker";
      if (!confirm(`"${nodeName}" worker node'unun token'ını yenilemek istediğinizden emin misiniz? Eski token ile bağlı olan agent bağlantısı kesilecektir.`)) {
        return;
      }
      button.disabled = true;
      try {
        const response = await fetch(`/api/admin/nodes/${nodeId}/rotate-token`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Token yenilenemedi.");
        renderWorkerTokenBox(data);
      } catch (error) {
        alert(error.message);
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll(".node-upgrade-btn").forEach(button => {
    button.addEventListener("click", async () => {
      const row = button.closest("tr");
      const nodeId = button.dataset.nodeId || row.dataset.nodeId;
      const nodeName = button.dataset.nodeName || "worker";
      if (!confirm(`"${nodeName}" worker node'una OTA yükseltme komutu gönderilsin mi?`)) {
        return;
      }
      button.disabled = true;
      button.textContent = "İletiliyor...";
      try {
        const response = await fetch(`/api/admin/nodes/${nodeId}/upgrade`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Yükseltme başlatılamadı.");
        alert(`✓ ${data.message}`);
      } catch (error) {
        alert(error.message);
      } finally {
        button.disabled = false;
        button.textContent = "⚡ Güncelle";
      }
    });
  });

  document.querySelectorAll(".node-delete-btn").forEach(button => {
    button.addEventListener("click", async () => {
      const row = button.closest("tr");
      const nodeId = button.dataset.nodeId || row.dataset.nodeId;
      const nodeName = button.dataset.nodeName || "worker";
      if (!confirm(`"${nodeName}" worker node'unu silmek istediğinizden emin misiniz?`)) {
        return;
      }
      button.disabled = true;
      try {
        const response = await fetch(`/api/admin/nodes/${nodeId}`, {
          method: "DELETE",
          headers: {"Content-Type": "application/json"},
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data.detail || `Worker silinemedi (${response.status})`);
        }
        window.location.reload();
      } catch (error) {
        alert(error.message);
        button.disabled = false;
      }
    });
  });

  // Node Labels Edit Modal
  const labelModal = document.getElementById("node-label-modal");
  const labelForm = document.getElementById("node-label-form");
  const labelInput = document.getElementById("label-modal-text");
  const labelNodeIdInput = document.getElementById("label-modal-node-id");
  const labelCloseBtn = document.getElementById("btn-close-label-modal");

  if (labelCloseBtn && labelModal) {
    labelCloseBtn.addEventListener("click", () => labelModal.classList.remove("open"));
  }

  document.querySelectorAll(".btn-edit-labels").forEach(button => {
    button.addEventListener("click", () => {
      const nodeId = button.dataset.nodeId;
      let rawLabels = button.dataset.labels || "{}";
      try {
        const parsed = JSON.parse(rawLabels);
        labelInput.value = JSON.stringify(parsed, null, 2);
      } catch (_) {
        labelInput.value = rawLabels;
      }
      labelNodeIdInput.value = nodeId;
      labelModal.classList.add("open");
    });
  });

  if (labelForm) {
    labelForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const nodeId = labelNodeIdInput.value;
      let labelsObj = {};
      const val = labelInput.value.trim();
      if (val) {
        if (val.startsWith("{")) {
          try {
            labelsObj = JSON.parse(val);
          } catch (err) {
            alert("Geçersiz JSON formatı: " + err.message);
            return;
          }
        } else {
          val.split("\n").forEach(line => {
            const [k, ...v] = line.split("=");
            if (k && v.length) labelsObj[k.trim()] = v.join("=").trim();
          });
        }
      }
      try {
        const response = await fetch(`/api/admin/nodes/${nodeId}/labels`, {
          method: "PUT",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({labels: labelsObj}),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Etiketler kaydedilemedi.");
        labelModal.classList.remove("open");
        window.location.reload();
      } catch (err) {
        alert(err.message);
      }
    });
  }
}

function initMlflowSettings() {
  const form = document.getElementById("mlflow-settings-form");
  if (!form) return;
  const testButton = document.getElementById("btn-test-mlflow");
  const saveButton = document.getElementById("btn-save-mlflow");
  const status = document.getElementById("mlflow-form-status");
  const badge = document.getElementById("mlflow-status-badge");

  function payload() {
    const data = new FormData(form);
    return {
      enabled: form.elements.enabled.checked,
      base_url: String(data.get("base_url") || "").trim(),
      auth_type: String(data.get("auth_type") || "none"),
      username: String(data.get("username") || "").trim(),
      secret: String(data.get("secret") || "") || null,
      validate_tls: form.elements.validate_tls.checked,
      ca_cert_file: String(data.get("ca_cert_file") || "").trim(),
      timeout_seconds: Number(data.get("timeout_seconds") || 10),
    };
  }

  async function send(url, method) {
    const response = await fetch(url, {
      method,
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload()),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `MLflow işlemi başarısız (${response.status})`);
    return data;
  }

  testButton.addEventListener("click", async () => {
    testButton.disabled = saveButton.disabled = true;
    status.textContent = "MLflow bağlantısı test ediliyor...";
    status.className = "quota-form-status";
    try {
      const result = await send("/api/admin/mlflow-settings/test", "POST");
      status.textContent = `${result.message} ${result.response_time_ms} ms`;
      status.className = "quota-form-status quota-status-success";
    } catch (error) {
      status.textContent = error.message;
      status.className = "quota-form-status quota-status-error";
    } finally {
      testButton.disabled = saveButton.disabled = false;
    }
  });

  form.addEventListener("submit", async event => {
    event.preventDefault();
    testButton.disabled = saveButton.disabled = true;
    status.textContent = "Kaydediliyor...";
    try {
      const result = await send("/api/admin/mlflow-settings", "PUT");
      form.elements.secret.value = "";
      form.elements.secret.placeholder = "Kayıtlı değeri korumak için boş bırakın";
      badge.className = `badge ${result.enabled ? "badge-running" : "badge-stopped"}`;
      badge.textContent = result.enabled ? "Etkin" : "Devre Dışı";
      status.textContent = "MLflow ayarları kaydedildi.";
      status.className = "quota-form-status quota-status-success";
    } catch (error) {
      status.textContent = error.message;
      status.className = "quota-form-status quota-status-error";
    } finally {
      testButton.disabled = saveButton.disabled = false;
    }
  });
}

// 6. Admin offline-download publisher
function initDownloadSettings() {
  const form = document.getElementById("download-settings-form");
  if (!form) return;
  const saveButton = document.getElementById("btn-save-download-settings");
  const status = document.getElementById("download-settings-status");
  const command = document.getElementById("worker-bootstrap-command");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const publicBaseUrl = String(form.elements.public_base_url.value || "")
      .trim()
      .replace(/\/+$/, "");
    saveButton.disabled = true;
    status.textContent = "Controller URL kaydediliyor...";
    status.className = "quota-form-status";
    try {
      const response = await fetch("/api/admin/download-settings", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({public_base_url: publicBaseUrl}),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `Controller URL kaydedilemedi (${response.status})`);
      }
      form.elements.public_base_url.value = data.public_base_url;
      if (command) {
        command.textContent = `# Önerilen: Controller'dan tek satırda kurun\ncurl -fsSL '${data.worker_bootstrap_url}' | sudo bash`;
      }
      status.textContent = "Controller URL kaydedildi; yeni bootstrap scriptleri bu adresi kullanacak.";
      status.className = "quota-form-status quota-status-success";
    } catch (error) {
      status.textContent = error.message;
      status.className = "quota-form-status quota-status-error";
    } finally {
      saveButton.disabled = false;
    }
  });
}

function initHttpsSettings() {
  const form = document.getElementById("https-settings-form");
  if (!form) return;
  const button = document.getElementById("btn-apply-https-settings");
  const status = document.getElementById("https-settings-status");
  const badge = document.getElementById("https-status-badge");
  const summary = document.getElementById("https-certificate-summary");
  const masterForm = document.getElementById("download-settings-form");
  const command = document.getElementById("worker-bootstrap-command");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const enabling = form.elements.https_enabled.checked;
    const hasCertificate = Boolean(form.elements.certificate.files[0]);
    const hasPrivateKey = Boolean(form.elements.private_key.files[0]);
    if (hasCertificate !== hasPrivateKey) {
      status.textContent = "Sertifika ve private key dosyalarını birlikte seçin.";
      status.className = "quota-form-status quota-status-error";
      return;
    }
    if (enabling && !hasCertificate && summary.dataset.uploaded !== "true" &&
        !summary.textContent.includes("Yüklü sertifika:")) {
      status.textContent = "HTTPS'i ilk kez açmak için sertifika ve private key yükleyin.";
      status.className = "quota-form-status quota-status-error";
      return;
    }

    const payload = new FormData(form);
    payload.set("https_enabled", enabling ? "true" : "false");
    payload.set(
      "http_fallback_enabled",
      form.elements.http_fallback_enabled.checked ? "true" : "false"
    );
    button.disabled = true;
    status.textContent = "Sertifika doğrulanıyor ve Nginx yapılandırılıyor...";
    status.className = "quota-form-status";
    try {
      const response = await fetch("/api/admin/download-settings/https", {
        method: "POST",
        body: payload,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `HTTPS ayarları uygulanamadı (${response.status})`);
      }
      form.elements.https_hostname.value = data.https_hostname;
      form.elements.https_enabled.checked = data.https_enabled;
      form.elements.http_fallback_enabled.checked = data.http_fallback_enabled;
      form.elements.certificate.value = "";
      form.elements.private_key.value = "";
      badge.className = `badge ${data.https_enabled ? "badge-running" : "badge-stopped"}`;
      badge.textContent = data.https_enabled ? "HTTPS Etkin" : "HTTP Etkin";
      if (data.certificate_uploaded) {
        summary.dataset.uploaded = "true";
        summary.textContent = `Yüklü sertifika: ${data.certificate_subject} · Son geçerlilik: ${data.certificate_not_after} · SHA-256: ${data.certificate_sha256}`;
      }
      if (masterForm) masterForm.elements.public_base_url.value = data.public_base_url;
      if (command) {
        command.textContent = `# Önerilen: Controller'dan tek satırda kurun\ncurl -fsSL '${data.worker_bootstrap_url}' | sudo bash`;
      }
      status.textContent = data.https_enabled
        ? "HTTPS etkinleştirildi. DNS ve istemci TCMB-CA güvenini ayrıca doğrulayın."
        : "HTTPS devre dışı; port 80 HTTP erişimi etkin.";
      status.className = "quota-form-status quota-status-success";
    } catch (error) {
      status.textContent = error.message;
      status.className = "quota-form-status quota-status-error";
    } finally {
      button.disabled = false;
    }
  });
}

function initDownloadUpdater() {
  const serverButton = document.getElementById("btn-update-downloads");
  const workerButton = document.getElementById("btn-update-worker-downloads");
  if (!serverButton && !workerButton) return;

  function badgeForState(state) {
    if (state === "success") return ["badge badge-running", "Hazır"];
    if (state === "failed") return ["badge badge-error", "Başarısız"];
    if (state === "running" || state === "queued") {
      return ["badge badge-creating", state === "queued" ? "Sırada" : "Oluşturuluyor"];
    }
    return ["badge badge-stopped", state === "disabled" ? "Devre Dışı" : "Beklemede"];
  }

  function createBundleController(config) {
    const button = document.getElementById(config.buttonId);
    if (!button) return null;
    const badge = document.getElementById(config.badgeId);
    const message = document.getElementById(config.messageId);
    const currentLink = document.getElementById(config.linkId);
    const logs = document.getElementById(config.logsId);
    let pollTimer = null;

    function renderStatus(data) {
      const effectiveState = data.enabled ? data.state : "disabled";
      const [badgeClass, badgeText] = badgeForState(effectiveState);
      badge.className = badgeClass;
      badge.textContent = badgeText;
      message.textContent = data.enabled
        ? (data.message || `${config.label} paketi oluşturulmaya hazır.`)
        : "İndirme güncellemeleri kapalı. Sunucuda sudo bash deploy/enable_downloads.sh komutunu çalıştırın.";

      const active = data.state === "queued" || data.state === "running";
      button.disabled = !data.enabled || active;
      button.textContent = active ? "Güncelleniyor..." : `${config.label} Paketini Güncelle`;

      if (data.current) {
        currentLink.href = data.current.download_url;
        currentLink.textContent = `${data.current.filename} (${data.current.size_display})`;
        currentLink.style.display = "inline";
      } else {
        currentLink.style.display = "none";
        currentLink.textContent = "";
      }

      const output = Array.isArray(data.logs) && data.logs.length
        ? data.logs.join("\n")
        : `Henüz ${config.label.toLowerCase()} paket logu yok.`;
      if (logs.textContent !== output) {
        logs.textContent = output;
        logs.scrollTop = logs.scrollHeight;
      }
      return active;
    }

    async function refreshStatus() {
      if (pollTimer) clearTimeout(pollTimer);
      try {
        const response = await fetch(config.statusUrl, { cache: "no-store" });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data.detail || `Durum isteği başarısız (${response.status})`);
        }
        const active = renderStatus(data);
        pollTimer = setTimeout(refreshStatus, active ? 2000 : 10000);
      } catch (error) {
        badge.className = "badge badge-error";
        badge.textContent = "Hata";
        message.textContent = error.message;
        button.disabled = true;
        pollTimer = setTimeout(refreshStatus, 10000);
      }
    }

    button.addEventListener("click", async () => {
      const confirmed = confirm(
        `${config.label} çevrim dışı paketi oluşturulup yayımlansın mı? Bu işlem sistem RPM'lerini indirir, beş container image'ını yeniden oluşturur ve birkaç dakika sürebilir.`
      );
      if (!confirmed) return;

      button.disabled = true;
      button.textContent = "Sıraya alınıyor...";
      try {
        const response = await fetch(config.updateUrl, { method: "POST" });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data.detail || `Güncelleme isteği başarısız (${response.status})`);
        }
        renderStatus(data);
        await refreshStatus();
      } catch (error) {
        badge.className = "badge badge-error";
        badge.textContent = "Hata";
        message.textContent = error.message;
        button.disabled = false;
        button.textContent = `${config.label} Paketini Güncelle`;
      }
    });

    refreshStatus();
    return { refreshStatus };
  }

  const controllers = [
    createBundleController({
      label: "Controller",
      buttonId: "btn-update-downloads",
      badgeId: "download-update-badge",
      messageId: "download-update-message",
      linkId: "download-current-link",
      logsId: "download-update-logs",
      statusUrl: "/api/admin/downloads/server/status",
      updateUrl: "/api/admin/downloads/server/update",
    }),
    createBundleController({
      label: "Worker",
      buttonId: "btn-update-worker-downloads",
      badgeId: "worker-download-update-badge",
      messageId: "worker-download-update-message",
      linkId: "worker-download-current-link",
      logsId: "worker-download-update-logs",
      statusUrl: "/api/admin/downloads/worker/status",
      updateUrl: "/api/admin/downloads/worker/update",
    }),
  ].filter(Boolean);

  const cleanButton = document.getElementById("btn-clean-downloads");
  if (cleanButton) {
    cleanButton.addEventListener("click", async () => {
      const confirmed = confirm(
        "Eski çevrim dışı kurulum paketleri ve geçici derleme dosyaları silinsin mi? Yalnızca en son yayımlanan paket korunacaktır."
      );
      if (!confirmed) return;

      cleanButton.disabled = true;
      cleanButton.textContent = "Temizleniyor...";
      try {
        const response = await fetch("/api/admin/downloads/clean", { method: "POST" });
        const res = await response.json();
        if (!response.ok) throw new Error(res.detail || "Temizleme başarısız.");
        alert(`Temizlik tamamlandı: ${res.cleaned_count} öğe silindi, ${res.freed_display} disk alanı kazanıldı.`);
        await Promise.all(controllers.map((controller) => controller.refreshStatus()));
      } catch (err) {
        alert(`Hata: ${err.message}`);
      } finally {
        cleanButton.disabled = false;
        cleanButton.textContent = "🧹 Eski Paketleri Temizle";
      }
    });
  }

}

// 6. Live Metrics Polling for Dashboard & Detail Views
function initLiveMetricsPolling() {
  const metricBoxes = document.querySelectorAll("[data-metrics-ws-id]");
  const detailCpu = document.getElementById("detail-val-cpu");
  const detailRam = document.getElementById("detail-val-ram");
  const detailRamPct = document.getElementById("detail-val-ram-pct");
  const detailDisk = document.getElementById("detail-val-disk");
  const detailUptime = document.getElementById("detail-val-uptime");
  const detailMetricsRoot = document.querySelector("[data-detail-metrics-ws-id]");
  const detailWorkspaceId = detailMetricsRoot?.dataset.detailMetricsWsId;

  if (!metricBoxes.length && !detailWorkspaceId) return;

  async function pollStats() {
    try {
      const statsMap = {};

      if (metricBoxes.length) {
        const res = await fetch("/api/workspaces/stats/summary", { cache: "no-store" });
        if (!res.ok) throw new Error(`Metrik özeti alınamadı (HTTP ${res.status})`);
        const data = await res.json();
        (data.stats || []).forEach((s) => {
          statsMap[s.workspace_id] = s;
        });
      }

      if (detailWorkspaceId) {
        const detailRes = await fetch(
          `/api/workspaces/${encodeURIComponent(detailWorkspaceId)}/stats`,
          { cache: "no-store" }
        );
        if (!detailRes.ok) {
          throw new Error(`Çalışma alanı metrikleri alınamadı (HTTP ${detailRes.status})`);
        }
        statsMap[detailWorkspaceId] = await detailRes.json();
      }

      metricBoxes.forEach((box) => {
        const wsId = box.dataset.metricsWsId;
        const st = statsMap[wsId];
        if (st) {
          const valCpu = box.querySelector(".val-cpu");
          const valRam = box.querySelector(".val-ram");
          const valDisk = box.querySelector(".val-disk");
          const valUptime = box.querySelector(".val-uptime");
          const barCpu = box.querySelector(".bar-cpu");
          const barRam = box.querySelector(".bar-ram");

          if (valCpu) valCpu.textContent = st.status === "running" ? `${st.cpu_percent}%` : "Kapalı";
          if (valRam) valRam.textContent = st.status === "running" ? st.mem_usage_display : "--";
          if (valDisk) valDisk.textContent = st.disk_usage_display || "--";
          if (valUptime) valUptime.textContent = st.uptime_display || "--";
          if (barCpu) barCpu.style.width = `${Math.min(st.cpu_percent * 2, 50)}%`;
          if (barRam) barRam.style.width = `${Math.min(st.mem_percent / 2, 50)}%`;
        }
      });

      const currentWsId = detailWorkspaceId;
      if (currentWsId && statsMap[currentWsId]) {
        const st = statsMap[currentWsId];
        if (detailCpu) detailCpu.textContent = st.status === "running" ? `${st.cpu_percent}%` : "Durduruldu";
        if (detailRam) detailRam.textContent = st.status === "running" ? st.mem_usage_display : "--";
        if (detailRamPct) detailRamPct.textContent = st.status === "running" ? `%${st.mem_percent} ayrılan RAM` : "--";
        if (detailDisk) detailDisk.textContent = st.disk_usage_display || "--";
        if (detailUptime) detailUptime.textContent = st.uptime_display || "0s";
      }
    } catch (err) {
      console.warn("Metrics polling error:", err);
      if (detailCpu) {
        detailCpu.textContent = "Metrik hatası";
        detailCpu.title = err.message;
      }
      if (detailRam) detailRam.textContent = "Yeniden deneniyor…";
      if (detailRamPct) detailRamPct.textContent = err.message;
    }
  }

  setInterval(pollStats, 4000);
  pollStats();
}

// 7. Workspace Detail Tabs
function initWorkspaceTabs() {
  const tabBtns = document.querySelectorAll(".tab-btn");
  if (!tabBtns.length) return;

  tabBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      tabBtns.forEach((b) => {
        b.classList.remove("active");
        b.style.color = "var(--text-muted)";
        b.style.borderBottom = "2px solid transparent";
      });
      document.querySelectorAll(".tab-content").forEach((c) => (c.style.display = "none"));

      btn.classList.add("active");
      btn.style.color = "var(--heading)";
      btn.style.borderBottom = "2px solid var(--primary)";

      const targetTab = document.getElementById(btn.dataset.tab);
      if (targetTab) {
        targetTab.style.display = "block";
        if (btn.dataset.tab === "tab-files") {
          DevCloudFileManager.init();
        }
      }
    });
  });
}

// 8. In-Browser File Manager
const DevCloudFileManager = {
  workspaceId: null,
  currentPath: "",

  init() {
    const container = document.getElementById("tab-files");
    if (!container) return;
    this.workspaceId = container.dataset.workspaceId;
    this.loadDir("");

    const fileInput = document.getElementById("fm-file-input");
    if (fileInput && !fileInput.dataset.bound) {
      fileInput.dataset.bound = "true";
      fileInput.addEventListener("change", () => this.handleUpload());
    }
  },

  async loadDir(path = "") {
    this.currentPath = path;
    const listBody = document.getElementById("fm-file-list");
    if (!listBody) return;

    listBody.innerHTML = '<tr><td colspan="4" style="padding: 1rem; text-align: center; color: var(--text-muted);">Yükleniyor...</td></tr>';

    try {
      const res = await fetch(`/api/workspaces/${this.workspaceId}/files?path=${encodeURIComponent(path)}`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        listBody.innerHTML = `<tr><td colspan="4" style="padding: 1rem; color: var(--danger); text-align: center;">${err.detail || "Dosyalar listelenemedi"}</td></tr>`;
        return;
      }

      const data = await res.json();
      this.renderBreadcrumbs(data.current_path);

      if (!data.items || !data.items.length) {
        listBody.innerHTML = '<tr><td colspan="4" style="padding: 1.5rem; text-align: center; color: var(--text-muted);">Bu klasör boş.</td></tr>';
        return;
      }

      let html = "";
      data.items.forEach((item) => {
        const icon = item.is_dir ? "📁" : "📄";
        const nameLink = item.is_dir
          ? `<a href="javascript:void(0)" onclick="DevCloudFileManager.loadDir('${item.path}')" style="color: #fff; font-weight: 600; text-decoration: none;">${icon} ${item.name}</a>`
          : `<span style="color: #cbd5e1;">${icon} ${item.name}</span>`;

        const downloadBtn = !item.is_dir
          ? `<a href="/api/workspaces/${this.workspaceId}/files/download?path=${encodeURIComponent(item.path)}" class="btn btn-secondary btn-sm" style="padding: 0.2rem 0.5rem; font-size: 0.72rem;">⬇</a>`
          : "";

        const deleteBtn = `<button class="btn btn-danger btn-sm" onclick="DevCloudFileManager.deleteItem('${item.path}')" style="padding: 0.2rem 0.5rem; font-size: 0.72rem;">🗑</button>`;

        html += `
          <tr style="border-bottom: 1px solid var(--border-color);">
            <td style="padding: 0.5rem 0.8rem;">${nameLink}</td>
            <td style="padding: 0.5rem 0.8rem; color: var(--text-muted);">${item.size_display}</td>
            <td style="padding: 0.5rem 0.8rem; color: var(--text-muted);">${item.modified_at}</td>
            <td style="padding: 0.5rem 0.8rem; text-align: right; display: flex; gap: 0.3rem; justify-content: flex-end;">${downloadBtn} ${deleteBtn}</td>
          </tr>
        `;
      });
      listBody.innerHTML = html;
    } catch (err) {
      listBody.innerHTML = `<tr><td colspan="4" style="padding: 1rem; color: var(--danger); text-align: center;">Hata: ${err.message}</td></tr>`;
    }
  },

  renderBreadcrumbs(curPath) {
    const el = document.getElementById("fm-breadcrumbs");
    if (!el) return;
    let html = `<span style="cursor: pointer; color: var(--primary);" onclick="DevCloudFileManager.loadDir('')">root</span>`;
    if (curPath) {
      const parts = curPath.split("/");
      let accum = "";
      parts.forEach((p) => {
        if (!p) return;
        accum += (accum ? "/" : "") + p;
        const thisPath = accum;
        html += ` <span style="color: var(--text-muted);">/</span> <span style="cursor: pointer; color: var(--primary);" onclick="DevCloudFileManager.loadDir('${thisPath}')">${p}</span>`;
      });
    }
    el.innerHTML = html;
  },

  async handleUpload() {
    const input = document.getElementById("fm-file-input");
    if (!input || !input.files.length) return;

    const formData = new FormData();
    formData.append("path", this.currentPath);
    for (let i = 0; i < input.files.length; i++) {
      formData.append("files", input.files[i]);
    }

    try {
      const res = await fetch(`/api/workspaces/${this.workspaceId}/files/upload`, {
        method: "POST",
        body: formData,
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(err.detail || "Dosya yüklenemedi.");
      }
      input.value = "";
      this.loadDir(this.currentPath);
    } catch (err) {
      alert("Yükleme hatası: " + err.message);
    }
  },

  async promptNewFolder() {
    const name = prompt("Yeni klasör adını girin:");
    if (!name || !name.trim()) return;
    const target = (this.currentPath ? this.currentPath + "/" : "") + name.trim();
    try {
      const res = await fetch(`/api/workspaces/${this.workspaceId}/files/mkdir?path=${encodeURIComponent(target)}`, { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(err.detail || "Klasör oluşturulamadı.");
      }
      this.loadDir(this.currentPath);
    } catch (err) {
      alert("Hata: " + err.message);
    }
  },

  async deleteItem(path) {
    if (!confirm(`'${path}' kalıcı olarak silinsin mi?`)) return;
    try {
      const res = await fetch(`/api/workspaces/${this.workspaceId}/files?path=${encodeURIComponent(path)}`, { method: "DELETE" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(err.detail || "Silinemedi.");
      }
      this.loadDir(this.currentPath);
    } catch (err) {
      alert("Silme hatası: " + err.message);
    }
  },
};

// 9. Custom Port Exposer Controller
function initPortExposer() {
  const btn = document.getElementById("btn-open-custom-port");
  const input = document.getElementById("input-custom-port");
  if (!btn || !input) return;

  const currentWsId = document.querySelector("[data-workspace-id]")?.dataset.workspaceId;
  btn.addEventListener("click", () => {
    const port = parseInt(input.value, 10);
    if (!port || port < 1 || port > 65535) {
      alert("Lütfen geçerli bir port numarası (1-65535) girin.");
      return;
    }
    window.open(`/proxy/${currentWsId}/port/${port}/`, "_blank");
  });
}

// 10. Snapshot Modal Controller
function initSnapshotModal() {
  const modal = document.getElementById("snapshot-modal");
  const openBtn = document.getElementById("btn-open-snapshot-modal");
  const closeBtn = document.getElementById("btn-close-snapshot-modal");
  const form = document.getElementById("snapshot-form");
  if (!modal || !openBtn) return;

  openBtn.addEventListener("click", () => modal.classList.add("open"));
  if (closeBtn) closeBtn.addEventListener("click", () => modal.classList.remove("open"));

  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const wsId = form.dataset.workspaceId;
      const name = document.getElementById("input-snap-name").value.trim();
      const desc = document.getElementById("input-snap-desc").value.trim();
      const submitBtn = document.getElementById("btn-submit-snapshot");

      submitBtn.disabled = true;
      submitBtn.textContent = "Şablon oluşturuluyor (podman commit)...";

      const fd = new FormData();
      fd.append("template_name", name);
      fd.append("template_description", desc);

      try {
        const res = await fetch(`/api/workspaces/${wsId}/snapshot`, {
          method: "POST",
          body: fd,
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.detail || "Şablon oluşturulamadı");
        }
        alert("🎉 " + data.message);
        modal.classList.remove("open");
      } catch (err) {
        alert("Snapshot hatası: " + err.message);
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = "Şablonu Kaydet (podman commit)";
      }
    });
  }
}

// 11. Admin 1-Click Platform Self-Updater & Custom Template Builder
function initAdminPlatformUpdater() {
  const commitCode = document.getElementById("admin-current-commit");
  const updateBtn = document.getElementById("btn-run-platform-update");
  const updateTerminalBox = document.getElementById("admin-update-terminal-box");
  const updateTerminal = document.getElementById("admin-update-terminal");

  const appendUpdateLine = (text) => {
    if (!updateTerminal) return;
    updateTerminal.textContent += text + "\n";
    updateTerminal.scrollTop = updateTerminal.scrollHeight;
  };

  const resetUpdateButton = () => {
    if (!updateBtn) return;
    updateBtn.disabled = false;
    updateBtn.textContent = "Platformu Güncelle";
  };

  const waitForPlatformReady = async (expectedVersion = null) => {
    appendUpdateLine("Servisin yeniden yüklenmesi ve sağlık kontrolü bekleniyor...");
    await new Promise((resolve) => setTimeout(resolve, 3500));

    const deadline = Date.now() + 90000;
    while (Date.now() < deadline) {
      try {
        const response = await fetch(`/api/admin/system/update-info?ts=${Date.now()}`, {
          cache: "no-store",
        });
        if (response.ok) {
          const info = await response.json();
          if (!expectedVersion || info.version === expectedVersion) {
            appendUpdateLine(`Servis hazır: v${info.version} (${info.commit})`);
            return true;
          }
        }
      } catch (_) {
        // A short connection failure is expected while systemd reloads workers.
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }

    appendUpdateLine("❌ Servis 90 saniye içinde doğrulanamadı. Restart logunu kontrol edin.");
    return false;
  };

  const reloadWithoutCache = () => {
    const target = new URL(window.location.href);
    target.searchParams.set("_updated", Date.now().toString());
    window.location.replace(target.toString());
  };

  if (commitCode) {
    fetch(`/api/admin/system/update-info?ts=${Date.now()}`, { cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((d) => {
        commitCode.textContent = `v${d.version} · ${d.branch} (${d.commit})`;
      })
      .catch(() => {
        commitCode.textContent = "Bağlantı hatası";
      });
  }

  if (updateBtn) {
    updateBtn.addEventListener("click", async () => {
      if (!confirm("Platform git sunucusundan en güncel sürüme yükseltilsin ve servis yeniden başlatılsın mı?")) return;

      updateBtn.disabled = true;
      updateBtn.textContent = "Güncelleniyor...";
      if (updateTerminalBox) updateTerminalBox.style.display = "block";
      if (updateTerminal) updateTerminal.textContent = "Güncelleme başlatılıyor...\n";

      let streamEstablished = false;
      try {
        const response = await fetch("/api/admin/system/update-stream", { method: "POST" });
        if (!response.ok) {
          const error = await response.json().catch(() => ({}));
          throw new Error(error.detail || `HTTP ${response.status}`);
        }
        if (!response.body) throw new Error("Sunucu canlı güncelleme akışı döndürmedi.");
        streamEstablished = true;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop();

          for (const line of lines) {
            if (line.startsWith("data: ")) {
              try {
                const data = JSON.parse(line.slice(6));
                if (data.type === "log") {
                  appendUpdateLine(data.text);
                } else if (data.type === "done") {
                  appendUpdateLine("\n🎉 " + data.text);
                  if (await waitForPlatformReady(data.version)) reloadWithoutCache();
                  else resetUpdateButton();
                  return;
                } else if (data.type === "error") {
                  appendUpdateLine("\n❌ " + data.text);
                  resetUpdateButton();
                  return;
                }
              } catch (_) {}
            }
          }
        }

        appendUpdateLine("Güncelleme akışı kapandı; servis durumu kontrol ediliyor...");
        if (await waitForPlatformReady()) reloadWithoutCache();
        else resetUpdateButton();
      } catch (err) {
        if (streamEstablished) {
          appendUpdateLine("Bağlantı yeniden yükleme sırasında kesildi; servis kontrol ediliyor...");
          if (await waitForPlatformReady()) reloadWithoutCache();
          else resetUpdateButton();
        } else {
          appendUpdateLine("\n❌ Güncelleme başlatılamadı: " + err.message);
          resetUpdateButton();
        }
      }
    });
  }

  const tbModal = document.getElementById("template-builder-modal");
  const tbOpenBtn = document.getElementById("btn-open-template-builder-modal");
  const tbCloseBtn = document.getElementById("btn-close-template-builder-modal");
  const tbForm = document.getElementById("template-builder-form");
  const tbTerminalBox = document.getElementById("tb-terminal-box");
  const tbTerminal = document.getElementById("tb-terminal");

  if (tbOpenBtn && tbModal) {
    tbOpenBtn.addEventListener("click", () => tbModal.classList.add("open"));
  }
  if (tbCloseBtn && tbModal) {
    tbCloseBtn.addEventListener("click", () => tbModal.classList.remove("open"));
  }

  if (tbForm) {
    tbForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const submitBtn = document.getElementById("btn-submit-tb");
      submitBtn.disabled = true;
      submitBtn.textContent = "Derleniyor (podman build)...";
      if (tbTerminalBox) tbTerminalBox.style.display = "block";
      if (tbTerminal) tbTerminal.textContent = "Container derlemesi başlatılıyor...\n";

      const fd = new FormData();
      fd.append("template_id", document.getElementById("tb-input-id").value.trim());
      fd.append("name", document.getElementById("tb-input-name").value.trim());
      fd.append("category", document.getElementById("tb-input-category").value.trim());
      fd.append("default_port", document.getElementById("tb-input-port").value);
      fd.append("ide_type", document.getElementById("tb-input-ide").value);
      fd.append("description", document.getElementById("tb-input-desc").value.trim());
      fd.append("containerfile", document.getElementById("tb-input-containerfile").value);

      try {
        const response = await fetch("/api/admin/templates/build-stream", {
          method: "POST",
          body: fd,
        });

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop();

          for (const line of lines) {
            if (line.startsWith("data: ")) {
              try {
                const data = JSON.parse(line.slice(6));
                if (data.type === "log") {
                  tbTerminal.textContent += data.text + "\n";
                  tbTerminal.scrollTop = tbTerminal.scrollHeight;
                } else if (data.type === "done") {
                  tbTerminal.textContent += "\n🎉 Şablon başarıyla derlendi ve kaydedildi!\n";
                  setTimeout(() => {
                    tbModal.classList.remove("open");
                    window.location.reload();
                  }, 2000);
                } else if (data.type === "error") {
                  tbTerminal.textContent += "\n❌ " + data.text + "\n";
                  submitBtn.disabled = false;
                  submitBtn.textContent = "Yeniden Dene";
                }
              } catch (_) {}
            }
          }
        }
      } catch (err) {
        tbTerminal.textContent += "\nHata: " + err.message + "\n";
        submitBtn.disabled = false;
        submitBtn.textContent = "Yeniden Dene";
      }
    });
  }
}
