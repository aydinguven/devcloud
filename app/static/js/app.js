// DevCloud Client Application Logic

document.addEventListener("DOMContentLoaded", () => {
  initWorkspaceCreationModal();
  initActionButtons();
  initLogPolling();
});

// 1. Workspace Creation Modal Handler
function initWorkspaceCreationModal() {
  const modalBackdrop = document.getElementById("create-modal");
  const openBtn = document.getElementById("btn-open-create-modal");
  const closeBtn = document.getElementById("btn-close-create-modal");
  const form = document.getElementById("create-workspace-form");

  if (!modalBackdrop) return;

  if (openBtn) {
    openBtn.addEventListener("click", () => {
      modalBackdrop.classList.add("open");
    });
  }

  if (closeBtn) {
    closeBtn.addEventListener("click", () => {
      modalBackdrop.classList.remove("open");
    });
  }

  // Click outside to close
  modalBackdrop.addEventListener("click", (e) => {
    if (e.target === modalBackdrop) {
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

  // Form Submit
  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const submitBtn = form.querySelector("button[type='submit']");
      const originalText = submitBtn.innerHTML;
      submitBtn.disabled = true;
      submitBtn.innerHTML = "Deploying Container...";

      const name = document.getElementById("input-workspace-name").value;
      const description = document.getElementById("input-workspace-desc")?.value || "";
      const templateId = templateInput.value;
      const flavorId = flavorInput.value;

      try {
        const res = await fetch("/api/workspaces", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: name,
            description: description,
            template_id: templateId,
            flavor_id: flavorId,
          }),
        });

        if (!res.ok) {
          const err = await res.json();
          alert(err.detail || "Failed to create workspace.");
          submitBtn.disabled = false;
          submitBtn.innerHTML = originalText;
          return;
        }

        const data = await res.json();
        // Redirect to detail / reload dashboard
        window.location.reload();
      } catch (err) {
        alert("Network error: " + err.message);
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalText;
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
        if (!confirm("Are you sure you want to delete this workspace? Container will be stopped and removed.")) {
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
