// DevCloud Client Application Logic

document.addEventListener("DOMContentLoaded", () => {
  initWorkspaceCreationModal();
  initActionButtons();
  initLogPolling();
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
        submitBtn.innerHTML = "Deploy Workspace";
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
        statusBadge.textContent = "Deploying...";
      }

      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="pulse-dot" style="margin-right: 0.35rem;"></span> Deploying...';
      }
      if (cancelBtn) cancelBtn.style.display = "none";

      appendLog(`Submitting request for '${name}' (${templateId}, ${flavorId})...`, "dim");

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
          appendLog(`Error (${response.status}): ${errData.detail || "Deployment request rejected"}`, "error");
          if (statusBadge) {
            statusBadge.className = "badge badge-error";
            statusBadge.textContent = "Failed";
          }
          if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.innerHTML = "Retry Deployment";
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
                statusBadge.textContent = "Error";
              }
              if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.innerHTML = "Retry Deployment";
              }
              if (cancelBtn) cancelBtn.style.display = "inline-flex";
            } else if (data.type === "done") {
              deploymentCompleted = true;
              if (statusBadge) {
                statusBadge.className = "badge badge-running";
                statusBadge.textContent = "Active";
              }
              appendLog("🎉 Container online & verified! Workspace is ready.", "success");

              // Replace footer with Launch & Dashboard buttons
              if (modalFooter) {
                modalFooter.innerHTML = `
                  <button type="button" class="btn btn-secondary" onclick="window.location.reload();">Back to Dashboard</button>
                  <a href="${data.web_url}" target="_blank" class="btn btn-success" style="padding: 0.65rem 1.5rem; font-weight: 600;">
                    <span>🚀</span> Launch IDE Now
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

        // Fallback if completed without explicit error
        if (!deploymentCompleted && statusBadge && statusBadge.textContent !== "Error") {
          setTimeout(() => {
            window.location.reload();
          }, 1000);
        }
      } catch (err) {
        appendLog(`Network connection error: ${err.message}`, "error");
        if (statusBadge) {
          statusBadge.className = "badge badge-error";
          statusBadge.textContent = "Network Error";
        }
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.innerHTML = "Retry Deployment";
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
        if (!confirm("Are you sure you want to permanently delete this workspace? The container and all persistent storage files will be erased.")) {
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
          alert(err.detail || `Action ${action} failed.`);
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
        alert("Action error: " + err.message);
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
        logTerminal.textContent = data.logs || "No logs yet.";
      }
    } catch (e) {
      console.warn("Failed to fetch container logs:", e);
    }
  }

  // Initial fetch and poll every 3 seconds
  fetchLogs();
  setInterval(fetchLogs, 3000);
}
