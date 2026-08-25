// ==============================================================================
// DevCloud Client Application Logic
// ==============================================================================

document.addEventListener("DOMContentLoaded", () => {
  initWorkspaceCreationModal();
  initActionButtons();
  initLogPolling();
  initQuotaForms();
  initDownloadUpdater();
  initLiveMetricsPolling();
  initWorkspaceTabs();
  initPortExposer();
  initSnapshotModal();
  initAdminPlatformUpdater();
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
      : "İndirme güncellemeleri bu kurulumda kapalı. Sunucuda sudo bash deploy/enable_downloads.sh komutunu çalıştırın.";

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
        await refreshStatus();
      } catch (err) {
        alert(`Hata: ${err.message}`);
      } finally {
        cleanButton.disabled = false;
        cleanButton.textContent = "🧹 Eski Paketleri Temizle";
      }
    });
  }

  refreshStatus();
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
