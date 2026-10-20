(() => {
  "use strict";

  const esc = value => String(value ?? "").replace(/[&<>'"]/g, character => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[character]);
  const request = async (url, options = {}) => {
    const response = await fetch(url, {cache: "no-store", headers: {"Content-Type": "application/json"}, ...options});
    const data = response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (!response.ok) {
      let detail = data.detail || `İstek başarısız (${response.status})`;
      if (Array.isArray(detail)) detail = detail.map(item => item.msg).join("; ");
      throw new Error(detail);
    }
    return data;
  };
  const setStatus = (element, message, kind = "") => {
    element.textContent = message;
    element.className = `quota-form-status${kind ? ` quota-status-${kind}` : ""}`;
  };

  function initDeployRegistry() {
    const form = document.getElementById("deploy-registry-settings-form");
    if (!form) return;
    const status = document.getElementById("deploy-registry-form-status");
    const badge = document.getElementById("deploy-registry-status-badge");
    const saveButton = document.getElementById("btn-save-deploy-registry");
    const testButton = document.getElementById("btn-test-deploy-registry");
    const payload = () => {
      const password = String(form.elements.password.value || "");
      return {
        enabled: form.elements.enabled.checked,
        registry_url: String(form.elements.registry_url.value || "").trim(),
        username: String(form.elements.username.value || "").trim(),
        password: form.elements.clear_password.checked ? "" : (password || null),
        tls_verify: form.elements.tls_verify.checked,
        manifest_format: form.elements.manifest_format.value,
      };
    };

    form.addEventListener("submit", async event => {
      event.preventDefault();
      saveButton.disabled = true;
      setStatus(status, "Kaydediliyor...");
      try {
        const saved = await request("/api/admin/deploy-registry-settings", {method: "PUT", body: JSON.stringify(payload())});
        form.elements.password.value = "";
        form.elements.clear_password.checked = false;
        form.elements.password.placeholder = saved.has_password ? "Kayıtlı parolayı korumak için boş bırakın" : "Registry parolasını girin";
        badge.className = `badge ${saved.enabled ? "badge-running" : "badge-stopped"}`;
        badge.textContent = saved.enabled ? "Etkin" : "Devre Dışı";
        setStatus(status, "Dağıtım registry ayarları şifreli olarak kaydedildi.", "success");
      } catch (error) {
        setStatus(status, error.message, "error");
      } finally {
        saveButton.disabled = false;
      }
    });

    testButton.addEventListener("click", async () => {
      testButton.disabled = true;
      setStatus(status, "Registry controller'dan test ediliyor...");
      const candidate = payload();
      try {
        const result = await request("/api/admin/deploy-registry-settings/test", {
          method: "POST",
          body: JSON.stringify({registry_url: candidate.registry_url, username: candidate.username, password: candidate.password, tls_verify: candidate.tls_verify}),
        });
        const latency = Number.isInteger(result.latency_ms) ? ` · ${result.latency_ms} ms` : "";
        setStatus(status, `${result.ok ? "✓" : "✗"} ${result.message}${latency}`, result.ok ? "success" : "error");
      } catch (error) {
        setStatus(status, error.message, "error");
      } finally {
        testButton.disabled = false;
      }
    });
  }

  function initProjects() {
    const form = document.getElementById("k8s-project-form");
    const list = document.getElementById("k8s-project-list");
    if (!form || !list) return;
    const status = document.getElementById("k8s-project-form-status");
    const saveButton = document.getElementById("btn-save-k8s-project");
    const cancelButton = document.getElementById("btn-cancel-k8s-project");
    let projects = [];

    const resetProjectForm = () => {
      form.reset();
      form.elements.project_id.value = "";
      form.elements.enabled.checked = true;
      form.elements.rfs_code.readOnly = false;
      form.elements.namespace.readOnly = false;
      saveButton.textContent = "Proje Ekle";
      cancelButton.hidden = true;
    };
    cancelButton.addEventListener("click", () => { resetProjectForm(); setStatus(status, ""); });

    const applicationRow = application => `<tr>
      <td><strong>${esc(application.app_name)}</strong>${application.description ? `<br><span class="text-muted">${esc(application.description)}</span>` : ""}</td>
      <td class="data-value"><code>${esc(application.repository)}</code></td>
      <td>${esc(application.mlflow_model_name)}</td>
      <td>${esc(application.last_version || "—")}<br><span class="text-muted">${application.release_count} yayın</span></td>
      <td><span class="badge ${application.enabled ? "badge-running" : "badge-stopped"}">${application.enabled ? "Etkin" : "Devre Dışı"}</span></td>
      <td style="white-space:nowrap;">
        ${application.bitbucket_url ? `<a class="btn btn-secondary btn-sm" href="${esc(application.bitbucket_url)}" target="_blank" rel="noopener noreferrer">Bitbucket ↗</a>` : ""}
        <button type="button" class="btn btn-secondary btn-sm" data-k8s-action="edit-app" data-id="${esc(application.id)}">Düzenle</button>
        ${application.locked ? "" : `<button type="button" class="btn btn-danger btn-sm" data-k8s-action="delete-app" data-id="${esc(application.id)}">Sil</button>`}
      </td>
    </tr>`;

    const projectBlock = project => `<article class="card" style="margin-top:1rem;padding:1rem;" data-project-id="${esc(project.id)}">
      <div class="card-header">
        <div>
          <h3 class="card-title" style="font-size:1rem;">${esc(project.name)}</h3>
          <p class="text-muted" style="margin:.25rem 0 0;">RFS <code>${esc(project.rfs_code)}</code> · namespace <code>${esc(project.namespace)}</code>${project.description ? ` · ${esc(project.description)}` : ""}</p>
        </div>
        <div style="display:flex;gap:.4rem;align-items:center;flex-wrap:wrap;">
          <span class="badge ${project.enabled ? "badge-running" : "badge-stopped"}">${project.enabled ? "Etkin" : "Devre Dışı"}</span>
          ${project.locked ? '<span class="badge badge-neutral" title="Yayınlanmış imajları olduğu için RFS kodu ve namespace kilitli">Kilitli</span>' : ""}
          <button type="button" class="btn btn-secondary btn-sm" data-k8s-action="edit-project" data-id="${esc(project.id)}">Düzenle</button>
          ${project.applications.length ? "" : `<button type="button" class="btn btn-danger btn-sm" data-k8s-action="delete-project" data-id="${esc(project.id)}">Sil</button>`}
        </div>
      </div>
      <div class="data-table-shell">
        <table class="table"><thead><tr><th>Uygulama</th><th>İmaj</th><th>MLflow modeli</th><th>Son versiyon</th><th>Durum</th><th>İşlem</th></tr></thead>
        <tbody>${project.applications.map(applicationRow).join("") || '<tr><td colspan="6" class="text-muted">Henüz uygulama yok.</td></tr>'}</tbody></table>
      </div>
      <form data-k8s-app-form="${esc(project.id)}" autocomplete="off" style="margin-top:.75rem;">
        <input type="hidden" name="application_id">
        <div class="grid grid-cols-2" style="gap:.75rem;margin-bottom:.75rem;">
          <label class="form-group" style="margin:0;"><span class="form-label">Uygulama adı</span><input class="form-input" name="app_name" required maxlength="100" pattern="[a-zA-Z0-9]([a-zA-Z0-9\\-]*[a-zA-Z0-9])?" placeholder="${esc(project.rfs_code)}ml-uygulama"></label>
          <label class="form-group" style="margin:0;"><span class="form-label">MLflow modeli</span><input class="form-input" name="mlflow_model_name" required maxlength="255" placeholder="Kayıtlı model adı"></label>
          <label class="form-group" style="margin:0;"><span class="form-label">Bitbucket repo (isteğe bağlı)</span><input class="form-input" name="bitbucket_url" type="url" maxlength="500"></label>
          <label class="form-group" style="margin:0;"><span class="form-label">Açıklama</span><input class="form-input" name="description" maxlength="2000"></label>
          <label style="display:flex;gap:.5rem;align-items:center;"><input type="checkbox" name="enabled" checked><span>Etkin</span></label>
        </div>
        <div class="table-actions">
          <button type="submit" class="btn btn-primary btn-sm" data-app-submit>Uygulama Ekle</button>
          <button type="button" class="btn btn-secondary btn-sm" data-app-cancel hidden>Düzenlemeyi İptal Et</button>
          <span class="quota-form-status" data-app-status role="status" aria-live="polite"></span>
        </div>
      </form>
    </article>`;

    const render = () => {
      list.innerHTML = projects.map(projectBlock).join("") || '<p class="text-muted">Henüz Kubernetes projesi tanımlanmadı.</p>';
    };
    const load = async () => {
      const data = await request("/api/admin/k8s-projects");
      projects = data.projects || [];
      render();
    };
    const findApplication = id => {
      for (const project of projects) {
        const application = project.applications.find(item => item.id === id);
        if (application) return {project, application};
      }
      return {};
    };
    const resetAppForm = appForm => {
      appForm.reset();
      appForm.elements.application_id.value = "";
      appForm.elements.enabled.checked = true;
      appForm.elements.app_name.readOnly = false;
      appForm.querySelector("[data-app-submit]").textContent = "Uygulama Ekle";
      appForm.querySelector("[data-app-cancel]").hidden = true;
    };

    form.addEventListener("submit", async event => {
      event.preventDefault();
      const projectId = form.elements.project_id.value;
      const body = {
        rfs_code: form.elements.rfs_code.value.trim(),
        name: form.elements.name.value.trim(),
        namespace: form.elements.namespace.value.trim(),
        description: form.elements.description.value.trim(),
        enabled: form.elements.enabled.checked,
      };
      saveButton.disabled = true;
      setStatus(status, "Kaydediliyor...");
      try {
        await request(projectId ? `/api/admin/k8s-projects/${encodeURIComponent(projectId)}` : "/api/admin/k8s-projects", {method: projectId ? "PUT" : "POST", body: JSON.stringify(body)});
        resetProjectForm();
        await load();
        setStatus(status, "Proje kaydedildi.", "success");
      } catch (error) {
        setStatus(status, error.message, "error");
      } finally {
        saveButton.disabled = false;
      }
    });

    list.addEventListener("submit", async event => {
      const appForm = event.target.closest("[data-k8s-app-form]");
      if (!appForm) return;
      event.preventDefault();
      const appStatus = appForm.querySelector("[data-app-status]");
      const applicationId = appForm.elements.application_id.value;
      const body = {
        app_name: appForm.elements.app_name.value.trim(),
        mlflow_model_name: appForm.elements.mlflow_model_name.value.trim(),
        bitbucket_url: appForm.elements.bitbucket_url.value.trim(),
        description: appForm.elements.description.value.trim(),
        enabled: appForm.elements.enabled.checked,
      };
      setStatus(appStatus, "Kaydediliyor...");
      try {
        await request(
          applicationId ? `/api/admin/k8s-applications/${encodeURIComponent(applicationId)}` : `/api/admin/k8s-projects/${encodeURIComponent(appForm.dataset.k8sAppForm)}/applications`,
          {method: applicationId ? "PUT" : "POST", body: JSON.stringify(body)},
        );
        await load();
      } catch (error) {
        setStatus(appStatus, error.message, "error");
      }
    });

    list.addEventListener("click", async event => {
      const cancel = event.target.closest("[data-app-cancel]");
      if (cancel) { resetAppForm(cancel.closest("form")); return; }
      const button = event.target.closest("[data-k8s-action]");
      if (!button) return;
      const {k8sAction: action, id} = button.dataset;
      if (action === "edit-project") {
        const project = projects.find(item => item.id === id);
        if (!project) return;
        form.elements.project_id.value = project.id;
        form.elements.rfs_code.value = project.rfs_code;
        form.elements.name.value = project.name;
        form.elements.namespace.value = project.namespace;
        form.elements.description.value = project.description;
        form.elements.enabled.checked = project.enabled;
        form.elements.rfs_code.readOnly = project.locked;
        form.elements.namespace.readOnly = project.locked;
        saveButton.textContent = "Projeyi Güncelle";
        cancelButton.hidden = false;
        form.scrollIntoView({behavior: "smooth", block: "start"});
        return;
      }
      if (action === "edit-app") {
        const {project, application} = findApplication(id);
        const appForm = project && list.querySelector(`[data-k8s-app-form="${CSS.escape(project.id)}"]`);
        if (!appForm) return;
        appForm.elements.application_id.value = application.id;
        appForm.elements.app_name.value = application.app_name;
        appForm.elements.app_name.readOnly = application.locked;
        appForm.elements.mlflow_model_name.value = application.mlflow_model_name;
        appForm.elements.bitbucket_url.value = application.bitbucket_url;
        appForm.elements.description.value = application.description;
        appForm.elements.enabled.checked = application.enabled;
        appForm.querySelector("[data-app-submit]").textContent = "Uygulamayı Güncelle";
        appForm.querySelector("[data-app-cancel]").hidden = false;
        appForm.scrollIntoView({behavior: "smooth", block: "center"});
        return;
      }
      const deleting = action === "delete-project" ? "Proje silinsin mi?" : "Uygulama silinsin mi?";
      if (!confirm(deleting)) return;
      button.disabled = true;
      try {
        await request(action === "delete-project" ? `/api/admin/k8s-projects/${encodeURIComponent(id)}` : `/api/admin/k8s-applications/${encodeURIComponent(id)}`, {method: "DELETE"});
        await load();
      } catch (error) {
        button.disabled = false;
        window.alert(error.message);
      }
    });

    load().catch(error => { list.innerHTML = `<p class="text-destructive">${esc(error.message)}</p>`; });
  }

  initDeployRegistry();
  initProjects();
})();
