// DevCloud Client Application Logic

document.addEventListener("DOMContentLoaded", () => {
  initWorkspaceCreationModal();
  initActionButtons();
  initLogPolling();
  initQuotaForms();
  initDownloadUpdater();
});

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
      // Reset state on open
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

  // Click outside to close (only if not actively deploying)
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

  // Form Submit with Real-time SSE Stream
  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();

      const name = document.getElementById("input-workspace-name").value.trim();
      const description = document.getElementById("input-workspace-desc")?.value.trim() || "";
      const templateId = templateInput.value;
      const flavorId = flavorInput.value;

      if (!name) return;

      // Show terminal box
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

      appendLog(`'${name}' için istek gönderiliyor (${templateId}, ${flavorId})...`, "dim");

      try {
        const response = await fetch("/api/workspaces/deploy-stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: name,
            description: description,
            template_id: templateId,
            flavor_id: flavorId,
          }),
        });

        if (!response.ok) {
          const errData = await response.json().catch(() => ({}));
          appendLog(`Hata (${response.status}): ${errData.detail || "Kurulum isteği reddedildi"}`, "error");
          if (statusBadge) {
            statusBadge.className = "badge badge-error";
            statusBadge.textContent = "Başarısız";
          }
          if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.innerHTML = "Kurulumu Yeniden Dene";
          }
          if (cancelBtn) cancelBtn.style.display = "inline-flex";
          return;
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

              // Auto-refresh dashboard after 2 seconds so active workspace is shown
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
          buffer = lines.pop(); // keep last incomplete chunk

          for (const line of lines) {
            processLine(line);
          }
        }

        // Process any remaining buffered text
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
          const err = await res.json();
          alert(err.detail || `${action} işlemi başarısız.`);
          btn.disabled = false;
          btn.innerHTML = originalHtml;
          return;
        }

        // If on workspace detail page, return to dashboard
        if (action === "delete" && window.location.pathname.startsWith("/workspaces/")) {
          window.location.href = "/";
          return;
        }

        // Reload page to reflect state
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

  // Initial fetch and poll every 3 seconds
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

// 5. Admin offline-download publisher
function initDownloadUpdater() {
  const button = document.getElementById("btn-update-downloads");
  if (!button) return;

  const badge = document.getElementById("download-update-badge");
  const message = document.getElementById("download-update-message");
  const currentLink = document.getElementById("download-current-link");
  const logs = document.getElementById("download-update-logs");
  let pollTimer = null;

  function badgeForState(state) {
    if (state === "success") return ["badge badge-running", "Hazır"];
    if (state === "failed") return ["badge badge-error", "Başarısız"];
    if (state === "running" || state === "queued") {
      return ["badge badge-creating", state === "queued" ? "Sırada" : "Oluşturuluyor"];
    }
    return ["badge badge-stopped", state === "disabled" ? "Devre Dışı" : "Beklemede"];
  }

  function renderStatus(data) {
    const effectiveState = data.enabled ? data.state : "disabled";
    const [badgeClass, badgeText] = badgeForState(effectiveState);
    badge.className = badgeClass;
    badge.textContent = badgeText;
    message.textContent = data.enabled
      ? (data.message || "Güncel paket oluşturulmaya hazır.")
      : "Güncellemeler devre dışı. Sunucuda DOWNLOADS_ENABLED ve DOWNLOAD_UPDATES_ENABLED ayarlarını etkinleştirin.";

    const active = data.state === "queued" || data.state === "running";
    button.disabled = !data.enabled || active;
    button.textContent = active ? "Güncelleniyor..." : "İndirmeleri Güncelle";

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
      : "Henüz güncelleme logu yok.";
    if (logs.textContent !== output) {
      logs.textContent = output;
      logs.scrollTop = logs.scrollHeight;
    }
    return active;
  }

  async function refreshStatus() {
    if (pollTimer) clearTimeout(pollTimer);
    try {
      const response = await fetch("/api/admin/downloads/status", { cache: "no-store" });
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
      "Güncel çevrim dışı paket oluşturulup yayımlansın mı? Bu işlem beş container image'ını yeniden oluşturur ve birkaç dakika sürebilir."
    );
    if (!confirmed) return;

    button.disabled = true;
    button.textContent = "Sıraya alınıyor...";
    try {
      const response = await fetch("/api/admin/downloads/update", { method: "POST" });
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
      button.textContent = "İndirmeleri Güncelle";
    }
  });

  refreshStatus();
}
