(() => {
  "use strict";

  if (document.body.dataset.onboardingUser !== "true") return;

  const TOPICS = [
    {
      id: "workspace-create",
      title: "Çalışma alanı oluşturma",
      prompt: "Yeni bir geliştirme ortamını nereden başlatacağınızı gösterir.",
      detail: "Yeni Çalışma Alanı düğmesi; şablon, kaynak profili ve çalışma süresi seçimlerini açar. Tur sizin adınıza çalışma alanı oluşturmaz.",
      route: () => "/",
      selector: '[data-tour="workspace-create-button"]',
    },
    {
      id: "resource-usage",
      title: "Kaynak ve kota görünümü",
      prompt: "CPU, RAM, disk ve GPU kullanımınızı nereden izleyeceğinizi gösterir.",
      detail: "Bu kartlar sistem kapasitesini, hesabınıza ayrılan kullanımı ve yeni çalışma alanları için kalan kotayı birlikte gösterir.",
      route: () => "/",
      selector: '[data-tour="quota-summary"]',
    },
    {
      id: "workspace-detail",
      title: "Çalışma alanı ayrıntıları",
      prompt: "Log, dosya yöneticisi ve port önizleme araçlarını tanıtır.",
      detail: "Çalışma alanı ayrıntılarında canlı metriklere, container loglarına, kalıcı dosyalara ve web uygulaması portlarına erişebilirsiniz.",
      route: state => state.context.first_workspace_url,
      selector: '[data-tour="workspace-tabs"]',
    },
    {
      id: "mlflow",
      title: "MLflow",
      prompt: "Deney, metrik, artifact ve model kayıt alanlarını tanıtır.",
      detail: "MLflow kullanıyorsanız kişisel bağlantınızı burada yapılandırabilir, deneyleri ve kayıtlı model sürümlerini inceleyebilirsiniz.",
      route: () => "/models",
      selector: '[data-tour="mlflow-personal-settings"]',
    },
    {
      id: "profile",
      title: "Kurumsal profil",
      prompt: "Dizinden eşitlenen profil bilgilerinizi ve turu yeniden başlatma seçeneğini gösterir.",
      detail: "Profil alanı kimlik ve organizasyon bilgilerinizi gösterir. Canlı turu daha sonra buradan tekrar başlatabilirsiniz.",
      route: () => "/profile",
      selector: '[data-tour="profile-card"]',
    },
    {
      id: "admin",
      title: "Yönetim paneli",
      prompt: "Yöneticiyseniz kullanıcı, worker, image ve sistem ayarlarının giriş noktasını gösterir.",
      detail: "Yönetim paneli operasyon alanlarını ayrı kategorilerde toplar. Bu konu yalnızca yönetici hesaplarına sunulur.",
      route: () => "/admin",
      selector: '[data-tour="admin-overview"]',
    },
  ];

  let state = null;
  let closeDialog = null;
  let closeHighlight = null;
  const restartButtons = [...document.querySelectorAll("[data-onboarding-restart]")];
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const fetchJson = async (url, options = {}) => {
    const response = await fetch(url, {
      cache: "no-store",
      ...options,
      headers: {
        ...(options.body ? {"Content-Type": "application/json"} : {}),
        ...(options.headers || {}),
      },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.detail || `Tur işlemi başarısız (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return data;
  };

  const refreshState = async () => {
    state = await fetchJson("/api/onboarding/state");
    syncRestartButtons();
    return state;
  };

  const patchState = async update => {
    try {
      state = await fetchJson("/api/onboarding/state", {
        method: "PATCH",
        body: JSON.stringify({
          tour_version: state.tour_version,
          expected_revision: state.revision,
          ...update,
        }),
      });
      syncRestartButtons();
      return state;
    } catch (error) {
      if (error.status === 409) await refreshState().catch(() => {});
      throw error;
    }
  };

  const availableTopics = () => TOPICS.filter(topic => state.features[topic.id] !== false);
  const topicById = topicId => TOPICS.find(topic => topic.id === topicId);
  const firstTopic = () => availableTopics()[0] || null;
  const nextTopic = topicId => {
    const start = TOPICS.findIndex(topic => topic.id === topicId);
    return TOPICS.slice(start + 1).find(topic => state.features[topic.id] !== false) || null;
  };

  const syncRestartButtons = () => {
    restartButtons.forEach(button => {
      button.hidden = !state?.enabled;
      if (!state?.enabled) return;
      const compact = Boolean(button.closest(".navbar"));
      if (state.status === "paused" || state.status === "in_progress") {
        button.textContent = compact ? "Tura Devam" : "Canlı Tura Devam Et";
      } else if (state.status === "completed") {
        button.textContent = compact ? "Turu Tekrarla" : "Canlı Turu Yeniden Başlat";
      } else {
        button.textContent = compact ? "Tur" : "Canlı Turu Başlat";
      }
    });
  };

  const closeSurfaces = () => {
    closeHighlight?.();
    closeHighlight = null;
    closeDialog?.();
    closeDialog = null;
  };

  const trapFocus = (container, event) => {
    if (event.key !== "Tab") return;
    const focusable = [...container.querySelectorAll(
      'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])'
    )].filter(element => !element.hidden);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const makeBackgroundInert = exceptions => {
    const exceptionSet = new Set(exceptions);
    const changed = [];
    [...document.body.children].forEach(element => {
      if (exceptionSet.has(element) || element.tagName === "SCRIPT") return;
      changed.push([element, element.inert]);
      element.inert = true;
    });
    return () => changed.forEach(([element, previous]) => { element.inert = previous; });
  };

  const openDialog = ({eyebrow, title, body, buttons, onEscape}) => {
    closeSurfaces();
    const previousFocus = document.activeElement;
    let restoreBackground = () => {};
    const backdrop = document.createElement("div");
    backdrop.className = "onboarding-tour-dialog-backdrop";
    const dialog = document.createElement("section");
    dialog.className = "onboarding-tour-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    const titleId = `onboarding-title-${Date.now()}`;
    const bodyId = `onboarding-body-${Date.now()}`;
    dialog.setAttribute("aria-labelledby", titleId);
    dialog.setAttribute("aria-describedby", bodyId);

    const eyebrowElement = document.createElement("span");
    eyebrowElement.className = "onboarding-tour-eyebrow";
    eyebrowElement.textContent = eyebrow;
    const heading = document.createElement("h2");
    heading.id = titleId;
    heading.textContent = title;
    const paragraph = document.createElement("p");
    paragraph.id = bodyId;
    paragraph.textContent = body;
    const error = document.createElement("p");
    error.className = "onboarding-tour-error";
    error.setAttribute("role", "alert");
    error.hidden = true;
    const actions = document.createElement("div");
    actions.className = "onboarding-tour-actions";

    let closed = false;
    const close = () => {
      if (closed) return;
      closed = true;
      backdrop.remove();
      restoreBackground();
      document.body.classList.remove("onboarding-tour-active");
      document.removeEventListener("keydown", keyHandler, true);
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
      if (closeDialog === close) closeDialog = null;
    };
    const run = async action => {
      actions.querySelectorAll("button").forEach(button => { button.disabled = true; });
      error.hidden = true;
      try {
        await action(close);
      } catch (actionError) {
        error.textContent = actionError.message;
        error.hidden = false;
        actions.querySelectorAll("button").forEach(button => { button.disabled = false; });
      }
    };
    buttons.forEach(config => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = config.className || "btn btn-secondary";
      button.textContent = config.label;
      if (config.primary) button.dataset.primary = "true";
      button.addEventListener("click", () => run(config.action));
      actions.appendChild(button);
    });
    const keyHandler = event => {
      if (event.key === "Escape" && onEscape) {
        event.preventDefault();
        run(async closeCurrent => {
          await onEscape();
          closeCurrent();
        });
        return;
      }
      trapFocus(dialog, event);
    };

    dialog.append(eyebrowElement, heading, paragraph, error, actions);
    backdrop.appendChild(dialog);
    document.body.appendChild(backdrop);
    restoreBackground = makeBackgroundInert([backdrop]);
    document.body.classList.add("onboarding-tour-active");
    document.addEventListener("keydown", keyHandler, true);
    closeDialog = close;
    (actions.querySelector("[data-primary]") || actions.querySelector("button"))?.focus();
    return close;
  };

  const pauseTour = async () => {
    if (state.enabled && state.status !== "completed") {
      await patchState({status: "paused"});
    }
    closeSurfaces();
  };

  const startTour = async () => {
    const topic = firstTopic();
    if (!topic) return finishTour();
    await patchState({
      status: "in_progress",
      current_topic: topic.id,
      current_step: "choice",
    });
    showTopicChoice(topic);
  };

  const showWelcome = () => {
    openDialog({
      eyebrow: "Canlı ürün turu",
      title: "DevCloud'u kendi ihtiyacınıza göre keşfedin",
      body: "Her konu başlamadan önce Göster veya Atla seçebilirsiniz. MLflow gibi kullanmadığınız alanları atlamak turun geri kalanını etkilemez.",
      onEscape: async () => {
        const topic = firstTopic();
        await patchState({
          status: "paused",
          current_topic: topic?.id || "",
          current_step: topic ? "choice" : "",
        });
      },
      buttons: [
        {
          label: "Şimdi Değil",
          className: "btn btn-secondary",
          action: async close => {
            const topic = firstTopic();
            await patchState({
              status: "paused",
              current_topic: topic?.id || "",
              current_step: topic ? "choice" : "",
            });
            close();
          },
        },
        {
          label: "Tura Başla",
          className: "btn btn-primary",
          primary: true,
          action: async close => {
            close();
            await startTour();
          },
        },
      ],
    });
  };

  const showTopicChoice = topic => {
    if (!topic || state.features[topic.id] === false) return advanceFrom(topic?.id || "");
    const topics = availableTopics();
    const topicNumber = topics.findIndex(item => item.id === topic.id) + 1;
    openDialog({
      eyebrow: `Konu ${topicNumber} / ${topics.length}`,
      title: topic.title,
      body: `${topic.prompt} Bu konuyu görmek ister misiniz?`,
      onEscape: pauseTour,
      buttons: [
        {
          label: "Atla",
          className: "btn btn-secondary",
          action: async close => {
            await patchState({
              status: "in_progress",
              current_topic: topic.id,
              current_step: "choice",
              topic_choice: {topic_id: topic.id, choice: "skip"},
            });
            close();
            await advanceFrom(topic.id);
          },
        },
        {
          label: "Göster",
          className: "btn btn-primary",
          primary: true,
          action: async close => {
            await patchState({
              status: "in_progress",
              current_topic: topic.id,
              current_step: "highlight",
              topic_choice: {topic_id: topic.id, choice: "show"},
            });
            close();
            await showHighlight(topic);
          },
        },
      ],
    });
  };

  const finishTour = async () => {
    await patchState({
      status: "completed",
      current_topic: "",
      current_step: "",
    });
    openDialog({
      eyebrow: "Tur tamamlandı",
      title: "Hazırsınız",
      body: "Seçtiğiniz DevCloud alanlarını tamamladınız. Turu üst menüden veya Profil sayfasından istediğiniz zaman yeniden başlatabilirsiniz.",
      buttons: [{label: "Kapat", className: "btn btn-primary", primary: true, action: async close => close()}],
    });
  };

  const advanceFrom = async topicId => {
    const following = nextTopic(topicId);
    if (!following) return finishTour();
    await patchState({
      status: "in_progress",
      current_topic: following.id,
      current_step: "choice",
    });
    showTopicChoice(following);
  };

  const waitForTarget = async (selector, timeout = 3500) => {
    const started = performance.now();
    while (performance.now() - started < timeout) {
      const target = document.querySelector(selector);
      if (target && target.getClientRects().length) return target;
      await new Promise(resolve => window.setTimeout(resolve, 100));
    }
    return null;
  };

  const showMissingTarget = topic => {
    openDialog({
      eyebrow: "Hedef bulunamadı",
      title: topic.title,
      body: "Bu alan şu anda sayfada kullanılamıyor. Yeniden deneyebilir veya yalnızca bu konuyu atlayabilirsiniz.",
      onEscape: pauseTour,
      buttons: [
        {
          label: "Konuyu Atla",
          className: "btn btn-secondary",
          action: async close => {
            await patchState({topic_choice: {topic_id: topic.id, choice: "skip"}});
            close();
            await advanceFrom(topic.id);
          },
        },
        {
          label: "Tekrar Dene",
          className: "btn btn-primary",
          primary: true,
          action: async close => {
            close();
            await showHighlight(topic);
          },
        },
      ],
    });
  };

  const createHighlight = (target, topic) => {
    closeSurfaces();
    const previousFocus = document.activeElement;
    const previousDescription = target.getAttribute("aria-describedby");
    const previousTabIndex = target.getAttribute("tabindex");
    const targetWasFocusable = target.matches('button, a[href], input, select, textarea, [tabindex]');
    if (!targetWasFocusable) target.setAttribute("tabindex", "-1");
    let restoreBackground = () => {};
    const descriptionId = `onboarding-highlight-${Date.now()}`;
    target.classList.add("onboarding-tour-target");
    target.setAttribute("aria-describedby", [previousDescription, descriptionId].filter(Boolean).join(" "));

    const scrims = Array.from({length: 4}, () => {
      const element = document.createElement("div");
      element.className = "onboarding-tour-scrim";
      document.body.appendChild(element);
      return element;
    });
    const callout = document.createElement("section");
    callout.className = "onboarding-tour-callout";
    callout.setAttribute("role", "dialog");
    callout.setAttribute("aria-modal", "true");
    const calloutTitleId = `onboarding-callout-title-${Date.now()}`;
    callout.setAttribute("aria-labelledby", calloutTitleId);
    callout.setAttribute("aria-describedby", descriptionId);
    const topics = availableTopics();
    const topicNumber = topics.findIndex(item => item.id === topic.id) + 1;
    const eyebrow = document.createElement("span");
    eyebrow.className = "onboarding-tour-eyebrow";
    eyebrow.textContent = `Gösteriliyor · ${topicNumber} / ${topics.length}`;
    const heading = document.createElement("h2");
    heading.id = calloutTitleId;
    heading.textContent = topic.title;
    const body = document.createElement("p");
    body.id = descriptionId;
    body.textContent = topic.detail;
    const progress = document.createElement("div");
    progress.className = "onboarding-tour-progress";
    progress.setAttribute("aria-hidden", "true");
    const progressValue = document.createElement("span");
    progressValue.style.width = `${Math.max(0, (topicNumber / topics.length) * 100)}%`;
    progress.appendChild(progressValue);
    const actions = document.createElement("div");
    actions.className = "onboarding-tour-actions";
    const pause = document.createElement("button");
    pause.type = "button";
    pause.className = "btn btn-secondary";
    pause.textContent = "Daha Sonra";
    const next = document.createElement("button");
    next.type = "button";
    next.className = "btn btn-primary";
    next.textContent = nextTopic(topic.id) ? "Devam" : "Turu Tamamla";
    actions.append(pause, next);
    callout.append(eyebrow, heading, body, progress, actions);
    document.body.appendChild(callout);
    restoreBackground = makeBackgroundInert([...scrims, callout]);
    document.body.classList.add("onboarding-tour-active");

    let closed = false;
    const update = () => {
      if (closed) return;
      if (!target.isConnected) {
        close();
        return;
      }
      const padding = 7;
      const rect = target.getBoundingClientRect();
      const top = Math.max(0, rect.top - padding);
      const left = Math.max(0, rect.left - padding);
      const right = Math.min(window.innerWidth, rect.right + padding);
      const bottom = Math.min(window.innerHeight, rect.bottom + padding);
      Object.assign(scrims[0].style, {left: "0px", top: "0px", width: "100vw", height: `${top}px`});
      Object.assign(scrims[1].style, {left: "0px", top: `${top}px`, width: `${left}px`, height: `${Math.max(0, bottom - top)}px`});
      Object.assign(scrims[2].style, {left: `${right}px`, top: `${top}px`, width: `${Math.max(0, window.innerWidth - right)}px`, height: `${Math.max(0, bottom - top)}px`});
      Object.assign(scrims[3].style, {left: "0px", top: `${bottom}px`, width: "100vw", height: `${Math.max(0, window.innerHeight - bottom)}px`});
      const calloutRect = callout.getBoundingClientRect();
      const calloutLeft = Math.min(Math.max(12, rect.left), Math.max(12, window.innerWidth - calloutRect.width - 12));
      const below = rect.bottom + 16;
      const calloutTop = below + calloutRect.height <= window.innerHeight - 12
        ? below
        : Math.max(12, rect.top - calloutRect.height - 16);
      callout.style.left = `${calloutLeft}px`;
      callout.style.top = `${calloutTop}px`;
    };
    const close = () => {
      if (closed) return;
      closed = true;
      scrims.forEach(element => element.remove());
      callout.remove();
      target.classList.remove("onboarding-tour-target");
      if (previousDescription) target.setAttribute("aria-describedby", previousDescription);
      else target.removeAttribute("aria-describedby");
      restoreBackground();
      document.body.classList.remove("onboarding-tour-active");
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
      window.removeEventListener("pagehide", close);
      document.removeEventListener("keydown", keyHandler, true);
      observer?.disconnect();
      if (target.isConnected) target.focus({preventScroll: true});
      else if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
      if (!targetWasFocusable) {
        if (previousTabIndex === null) target.removeAttribute("tabindex");
        else target.setAttribute("tabindex", previousTabIndex);
      }
      if (closeHighlight === close) closeHighlight = null;
    };
    const keyHandler = event => {
      if (event.key === "Escape") {
        event.preventDefault();
        pauseTour().catch(showFatalError);
        return;
      }
      trapFocus(callout, event);
    };
    pause.addEventListener("click", () => pauseTour().catch(showFatalError));
    next.addEventListener("click", async () => {
      next.disabled = true;
      pause.disabled = true;
      close();
      try {
        await advanceFrom(topic.id);
      } catch (error) {
        showFatalError(error);
      }
    });
    const observer = window.ResizeObserver ? new ResizeObserver(update) : null;
    observer?.observe(target);
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    window.addEventListener("pagehide", close);
    document.addEventListener("keydown", keyHandler, true);
    closeHighlight = close;
    update();
    next.focus();
  };

  const showHighlight = async topic => {
    const route = topic.route(state);
    if (!route) return showMissingTarget(topic);
    if (window.location.pathname !== route) {
      window.location.assign(route);
      return;
    }
    const target = await waitForTarget(topic.selector);
    if (!target) return showMissingTarget(topic);
    target.scrollIntoView({behavior: reducedMotion ? "auto" : "smooth", block: "center", inline: "nearest"});
    window.setTimeout(() => createHighlight(target, topic), reducedMotion ? 0 : 220);
  };

  const resumeTour = async () => {
    let topic = topicById(state.current_topic);
    if (!topic || state.features[topic.id] === false) {
      topic = topic ? nextTopic(topic.id) : firstTopic();
      if (!topic) return finishTour();
      await patchState({current_topic: topic.id, current_step: "choice", status: "in_progress"});
    }
    if (state.current_step === "highlight") await showHighlight(topic);
    else showTopicChoice(topic);
  };

  const showFatalError = async error => {
    try {
      if (state?.enabled && state.status === "in_progress") {
        await patchState({status: "paused"});
      }
    } catch (_pauseError) {
      // The original error remains the most useful message for the user.
    }
    closeSurfaces();
    openDialog({
      eyebrow: state?.status === "paused" ? "Tur duraklatıldı" : "Tur hatası",
      title: "Tur devam ettirilemedi",
      body: error.message || "Beklenmeyen bir hata oluştu. Turu üst menüden yeniden deneyebilirsiniz.",
      buttons: [{label: "Kapat", className: "btn btn-primary", primary: true, action: async close => close()}],
    });
  };

  const handleRestart = async () => {
    closeSurfaces();
    try {
      if (state.status === "paused") {
        await patchState({status: "in_progress"});
        await resumeTour();
        return;
      }
      if (state.status === "in_progress") {
        await resumeTour();
        return;
      }
      state = await fetchJson("/api/onboarding/restart", {
        method: "POST",
        body: JSON.stringify({
          tour_version: state.tour_version,
          expected_revision: state.revision,
        }),
      });
      syncRestartButtons();
      if (!state.enabled) return;
      await startTour();
    } catch (error) {
      showFatalError(error);
    }
  };

  restartButtons.forEach(button => button.addEventListener("click", handleRestart));

  document.addEventListener("DOMContentLoaded", async () => {
    try {
      await refreshState();
      if (!state.enabled) return;
      if (state.status === "in_progress") await resumeTour();
      else if (state.status === "not_started" && state.auto_offer) showWelcome();
    } catch (error) {
      console.warn("Onboarding tour could not initialize", error);
    }
  });
})();
