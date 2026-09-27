(() => {
  "use strict";

  const body = document.body;
  const liveRegion = document.getElementById("studio-live-region");
  const streamManagers = new Map();
  const aiQueueConfirmationTriggers = new WeakMap();
  const aiPlanInspectorStates = new WeakMap();
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  const announce = (message) => {
    if (!liveRegion || !message) return;
    liveRegion.textContent = "";
    window.setTimeout(() => {
      liveRegion.textContent = message;
    }, 20);
  };

  const setText = (element, value) => {
    if (element) element.textContent = value;
  };

  const safeSameOriginLocation = (value) => {
    if (!value) return null;
    try {
      const target = new URL(value, window.location.href);
      return target.origin === window.location.origin ? target : null;
    } catch (_error) {
      return null;
    }
  };

  const createServerFragment = (html, expectedSelector) => {
    if (typeof html !== "string" || !html.trim()) {
      throw new Error("empty server fragment");
    }
    const template = document.createElement("template");
    template.innerHTML = html;
    template.content.querySelectorAll("script, iframe, object, embed").forEach((node) => node.remove());
    template.content.querySelectorAll("*").forEach((element) => {
      for (const attribute of Array.from(element.attributes)) {
        const name = attribute.name.toLowerCase();
        const value = attribute.value.trim().toLowerCase();
        if (name.startsWith("on") || ((name === "href" || name === "src" || name === "action") && value.startsWith("javascript:"))) {
          element.removeAttribute(attribute.name);
        }
      }
    });
    if (!template.content.querySelector(expectedSelector)) {
      throw new Error("unexpected server fragment");
    }
    return template.content.cloneNode(true);
  };

  const capturePanelState = (panel) => {
    const scroll = new Map();
    panel.querySelectorAll("[data-scroll-key]").forEach((element) => {
      scroll.set(element.dataset.scrollKey, element.scrollTop);
    });
    const active = document.activeElement;
    const focusOwner = active && panel.contains(active) ? active.closest("[data-focus-key]") : null;
    return { scroll, focusKey: focusOwner?.dataset.focusKey || null };
  };

  const restorePanelState = (panel, state) => {
    panel.querySelectorAll("[data-scroll-key]").forEach((element) => {
      if (state.scroll.has(element.dataset.scrollKey)) {
        element.scrollTop = state.scroll.get(element.dataset.scrollKey);
      }
    });
    if (!state.focusKey) return;
    const owner = Array.from(panel.querySelectorAll("[data-focus-key]")).find(
      (element) => element.dataset.focusKey === state.focusKey,
    );
    if (!owner) return;
    const target = owner.matches("button, input, [tabindex]")
      ? owner
      : owner.querySelector("button, input, [tabindex]");
    target?.focus({ preventScroll: true });
  };

  const locateCurrentStage = (panel, behavior = "smooth") => {
    const viewport = panel?.querySelector("[data-stage-viewport]");
    const current = viewport?.querySelector("[data-current-stage-row]");
    if (!viewport || !current) return;
    const targetTop = current.offsetTop - (viewport.clientHeight - current.offsetHeight) / 2;
    viewport.scrollTo({
      top: Math.max(0, targetTop),
      behavior: reducedMotion.matches ? "auto" : behavior,
    });
  };

  const streamStatusElements = (panel) => {
    const elements = Array.from(panel.querySelectorAll("[data-stream-status]"));
    if (panel.dataset.eventPanel === "import") {
      const external = panel.closest(".import-check-board")?.querySelector("[data-external-stream-status]");
      if (external) elements.push(external);
    }
    return elements;
  };

  const setStreamState = (panel, state, message) => {
    streamStatusElements(panel).forEach((element) => {
      element.dataset.state = state;
      setText(element.querySelector("span"), message);
    });
  };

  const runIsSettled = (snapshot, fragment) => {
    const status = snapshot?.status || fragment?.dataset.runStatus || "pending";
    const boundary = snapshot?.boundary_event || fragment?.dataset.boundaryEvent;
    return Boolean(boundary) || ["paused", "needs_review", "failed", "succeeded", "cancelled"].includes(status);
  };

  const importIsSettled = (snapshot, fragment) => {
    const status = snapshot?.status || fragment?.dataset.checkStatus || "pending";
    const workspaceStatus = snapshot?.workspace?.status || fragment?.dataset.workspaceStatus || "absent";
    if (["pending", "running", "creating"].includes(workspaceStatus)) return false;
    return Boolean(snapshot?.can_import) || fragment?.dataset.terminal === "true" || [
      "succeeded", "failed", "interrupted",
    ].includes(status);
  };

  class SnapshotStream {
    constructor(panel) {
      this.panel = panel;
      this.kind = panel.dataset.eventPanel;
      this.url = panel.dataset.eventsUrl;
      this.source = null;
      this.hiddenPause = false;
      this.decisionPause = false;
      this.settled = false;
      this.openedOnce = false;
      this.currentStage = panel.dataset.initialStage || panel.dataset.initialPhase || null;
      this.currentStatus = panel.dataset.initialStatus || "pending";
      this.currentBoundary = panel.dataset.initialBoundary || "";
      this.initialTerminal = panel.dataset.initialTerminal === "true";
      this.initialWorkspaceStatus = panel.dataset.initialWorkspaceStatus || "absent";
      this.currentWorkspaceStatus = this.initialWorkspaceStatus;
      this.handleSnapshot = this.handleSnapshot.bind(this);
    }

    initialIsSettled() {
      if (this.kind === "run") {
        return Boolean(this.currentBoundary) || ["paused", "needs_review", "failed", "succeeded", "cancelled"].includes(this.currentStatus);
      }
      if (["pending", "running", "creating"].includes(this.initialWorkspaceStatus)) return false;
      return this.initialTerminal || ["succeeded", "failed", "interrupted"].includes(this.currentStatus);
    }

    open() {
      if (!this.url || this.source || this.settled || this.decisionPause || document.hidden || !("EventSource" in window)) return;
      setStreamState(this.panel, "connecting", "正在连接实时状态");
      const source = new EventSource(this.url);
      this.source = source;
      source.addEventListener("snapshot", this.handleSnapshot);
      source.onmessage = this.handleSnapshot;
      source.onopen = () => {
        this.openedOnce = true;
        setStreamState(this.panel, "connected", "实时状态已连接");
      };
      source.onerror = () => {
        if (this.source !== source || this.settled || this.hiddenPause || this.decisionPause) return;
        setStreamState(this.panel, "reconnecting", "连接中断，正在重连");
      };
    }

    close(message = "状态流已结束") {
      if (this.source) {
        this.source.close();
        this.source = null;
      }
      setStreamState(this.panel, "closed", message);
    }

    pauseForVisibility() {
      if (this.settled) return;
      this.hiddenPause = true;
      this.close("页面位于后台，实时连接已暂停");
      setStreamState(this.panel, "paused", "页面位于后台，实时连接已暂停");
    }

    resumeForVisibility() {
      if (this.settled) return;
      this.hiddenPause = false;
      this.open();
    }

    pauseForDecision() {
      this.decisionPause = true;
      this.close("等待批次操作确认，实时刷新已暂停");
    }

    resumeAfterDecision() {
      this.decisionPause = false;
      this.open();
    }

    restartAfterCommand() {
      this.settled = false;
      this.hiddenPause = false;
      this.decisionPause = false;
      this.close("等待命令后的最新状态");
      window.setTimeout(() => this.open(), 120);
    }

    handleSnapshot(event) {
      if (document.hidden || this.decisionPause) return;
      let packet;
      try {
        packet = JSON.parse(event.data);
      } catch (_error) {
        setStreamState(this.panel, "reconnecting", "状态数据无效，等待完整快照");
        return;
      }
      const snapshot = packet?.snapshot;
      const html = packet?.html;
      if (!snapshot || typeof html !== "string") {
        setStreamState(this.panel, "reconnecting", "状态快照不完整，等待重连");
        return;
      }

      const expectedSelector = this.kind === "run" ? "[data-run-fragment]" : "[data-import-fragment]";
      const oldStage = this.currentStage;
      const oldStatus = this.currentStatus;
      const newStage = this.kind === "run"
        ? snapshot.current_stage
        : (snapshot.phase || snapshot.current_phase);
      const newStatus = snapshot.status || oldStatus;
      const oldWorkspaceStatus = this.currentWorkspaceStatus;
      const newWorkspaceStatus = snapshot?.workspace?.status || oldWorkspaceStatus;
      const state = capturePanelState(this.panel);

      try {
        const fragment = createServerFragment(html, expectedSelector);
        this.panel.querySelectorAll("[data-ai-queue-confirmation]").forEach(clearAIPlanInspector);
        this.panel.replaceChildren(fragment);
        window.htmx?.process(this.panel);
        restorePanelState(this.panel, state);
        const rendered = this.panel.querySelector(expectedSelector);
        const resolvedStage = newStage || (this.kind === "run" ? rendered?.dataset.currentStage : rendered?.dataset.checkPhase);
        if (resolvedStage && resolvedStage !== oldStage && this.kind === "run") {
          locateCurrentStage(this.panel);
        }
        this.currentStage = resolvedStage || oldStage;
        this.currentStatus = newStatus;
        this.currentWorkspaceStatus = newWorkspaceStatus;
        this.currentBoundary = snapshot.boundary_event || rendered?.dataset.boundaryEvent || "";
        setStreamState(this.panel, "connected", "实时状态已连接");

        if (
          this.kind === "run"
          && ["R3_BOUNDARY_REACHED_BUILD_RELEASE_PENDING", "RELEASE_BUILT"].includes(this.currentBoundary)
          && !document.querySelector("[data-release-candidate]")
        ) {
          announce("运行已到候选构建边界，正在打开候选构建面板。 ");
          window.setTimeout(() => window.location.reload(), 180);
          return;
        }

        if (oldStage && this.currentStage && oldStage !== this.currentStage) {
          announce(`${this.kind === "run" ? "运行阶段" : "检查阶段"}已切换到 ${this.currentStage}。`);
        }
        if (newStatus === "failed" && oldStatus !== "failed") {
          announce(`${this.kind === "run" ? "运行" : "检查"}失败，请查看稳定错误码。`);
        } else if (newStatus !== oldStatus && ["succeeded", "passed", "cancelled", "needs_review", "paused"].includes(newStatus)) {
          announce(`${this.kind === "run" ? "运行" : "检查"}状态已变为 ${newStatus}。`);
        }
        if (this.kind === "import" && newWorkspaceStatus !== oldWorkspaceStatus) {
          if (["pending", "running", "creating"].includes(newWorkspaceStatus)) announce("已保留运行，正在创建工作区。");
          if (["created", "imported", "succeeded", "existing"].includes(newWorkspaceStatus)) announce("运行已创建，可以直接进入。");
          if (newWorkspaceStatus === "failed") announce("工作区创建失败，请查看稳定错误码。");
        }

        const settled = this.kind === "run"
          ? runIsSettled(snapshot, rendered)
          : importIsSettled(snapshot, rendered);
        if (settled) {
          this.settled = true;
          this.close(this.currentBoundary ? "已到当前阶段边界" : "状态流已结束");
        }
      } catch (_error) {
        setStreamState(this.panel, "reconnecting", "无法应用状态快照，等待重连");
      }
    }
  }

  const initializeSnapshotStreams = () => {
    document.querySelectorAll("[data-event-panel]").forEach((panel) => {
      const manager = new SnapshotStream(panel);
      streamManagers.set(panel.id, manager);
      if (manager.initialIsSettled()) {
        manager.settled = true;
        setStreamState(panel, "closed", panel.dataset.initialBoundary ? "已到当前阶段边界" : "状态流已结束");
      } else {
        manager.open();
      }
    });
  };

  const directoryFeedback = (form, state, message, errorCode = "") => {
    const feedback = form.querySelector("[data-directory-feedback]");
    const display = form.querySelector("[data-directory-display]");
    if (!feedback) return;
    feedback.className = `directory-feedback directory-feedback--${state}`;
    feedback.replaceChildren();
    const mark = document.createElement("span");
    mark.className = "directory-feedback__mark";
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = state === "ready" ? "✓" : state === "checking" ? "↻" : state === "neutral" ? "○" : "!";
    const copy = document.createElement("span");
    copy.textContent = errorCode ? `${errorCode} · ${message}` : message;
    feedback.append(mark, copy);
    const invalid = state === "invalid" || state === "mismatch";
    display?.setAttribute("aria-invalid", invalid ? "true" : "false");
  };

  const storedOperation = (key) => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(key) || "null");
      return typeof saved?.id === "string" ? saved : null;
    } catch (_) { return null; }
  };

  // Build inputs are stable. Import directory references are not; import retries
  // resolve their stored identity against the server before any new POST.
  const operationId = (key, prefix, inputs) => {
    const signature = JSON.stringify(inputs);
    let saved = storedOperation(key);
    if (!saved || saved.signature !== signature) {
      saved = { signature, id: prefix + crypto.randomUUID().replaceAll("-", "") };
      sessionStorage.setItem(key, JSON.stringify(saved));
    }
    return saved.id;
  };

  const initializeDirectoryChooser = (form) => {
    const retry = new URLSearchParams(window.location.search).get("retry_run_id");
    if (retry && /^run_[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(retry)) form.dataset.retryRunId = retry;
    const version = form.querySelector("[data-directory-version]");
    const reference = form.querySelector("[data-directory-ref]");
    const display = form.querySelector("[data-directory-display]");
    const submit = form.querySelector("[data-import-submit]");
    const chooser = form.querySelector("[data-directory-chooser]");
    const browse = form.querySelector("[data-directory-browse]");
    const entries = form.querySelector("[data-directory-entries]");
    const parent = form.querySelector("[data-directory-parent]");
    const status = form.querySelector("[data-directory-browser-status]");
    let parentRef = "";
    let generation = 0;
    const close = () => { chooser.hidden = true; browse.setAttribute("aria-expanded", "false"); browse.focus(); };
    const select = (entry) => {
      reference.value = entry.directory_ref;
      form.dataset.selectedExportId = entry.export_id || "";
      display.value = `${version.value} / ${entry.export_id || entry.label || "已选择导出"}`;
      submit.disabled = false;
      directoryFeedback(form, "ready", "已选择导出，导入时将验证并创建工作区。");
      close();
    };
    const load = async (ref = "") => {
      if (!version.reportValidity()) return;
      const request = ++generation;
      chooser.hidden = false;
      browse.setAttribute("aria-expanded", "true");
      entries.replaceChildren();
      setText(status, "正在读取导出目录…");
      try {
        const data = await fetchJsonEnvelope(`/api/directories?minecraft_version=${encodeURIComponent(version.value)}${ref ? `&parent_ref=${encodeURIComponent(ref)}` : ""}`);
        if (request !== generation) return;
        parentRef = data.parent_ref || "";
        parent.hidden = !parentRef;
        setText(form.querySelector("[data-directory-location]"), data.label || `Minecraft ${version.value} 导出`);
        const list = data.entries || [];
        setText(status, list.length ? `${list.length} 个目录项` : "所选位置没有可用导出。");
        list.forEach((entry) => {
          const row = document.createElement("article");
          row.className = "directory-entry";
          const identity = document.createElement("div");
          identity.className = "directory-entry__identity";
          const name = document.createElement("b");
          name.textContent = entry.export_id || entry.label || entry.name || "目录";
          const detail = document.createElement("small");
          detail.textContent = `${entry.minecraft_version || version.value} · ${entry.error_code || entry.preflight_status || "导出"}`;
          identity.append(name, detail);
          row.append(identity);
          if (entry.directory_ref && (entry.selectable || entry.can_enter)) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "button button--quiet";
            button.textContent = entry.selectable ? "选择导出" : "打开目录";
            button.addEventListener("click", () => entry.selectable ? select(entry) : load(entry.directory_ref));
            row.append(button);
          }
          entries.append(row);
        });
        entries.querySelector("button")?.focus();
      } catch (error) {
        if (request !== generation) return;
        setText(status, `${error.code || "DIRECTORY_BROWSER_UNAVAILABLE"} · 无法读取目录，请重试。`);
      }
    };
    browse.addEventListener("click", () => load());
    parent.addEventListener("click", () => load(parentRef));
    form.querySelector("[data-directory-close]").addEventListener("click", close);
    chooser.addEventListener("keydown", (event) => { if (event.key === "Escape") close(); });
    version.addEventListener("input", () => { ++generation; reference.value = ""; display.value = ""; delete form.dataset.selectedExportId; submit.disabled = true; });
    form.querySelector("[data-use-manual-ref]").addEventListener("click", () => {
      const ref = form.querySelector("[data-manual-directory-ref]").value.trim();
      if (ref) select({ directory_ref: ref, label: "手动目录引用" });
    });
    if (form.dataset.retryRunId || storedOperation("blockpedia.import")) {
      setText(form.querySelector("[data-import-start-status]"), "已有导入操作。继续时会先读取状态；更换导出或版本，请先开始新的导入操作。");
      setText(form.querySelector("[data-selected-action-label]"), "继续本次导入");
    }
    form.querySelector("[data-new-import]").addEventListener("click", () => {
      sessionStorage.removeItem("blockpedia.import");
      delete form.dataset.retryRunId;
      window.history.replaceState(null, "", window.location.pathname);
      setText(form.querySelector("[data-import-start-status]"), "已开始新操作，下次提交将创建新的导入。");
      setText(form.querySelector("[data-selected-action-label]"), "开始导入");
    });
  };

  const performSelectedAction = async (form) => {
    if (form.dataset.busy === "true" || !form.reportValidity()) return;
    const source_directory_ref = form.querySelector("[data-directory-ref]").value;
    const minecraft_version = form.querySelector("[data-directory-version]").value;
    const feature_workers = Number(form.elements.feature_workers.value);
    if (!source_directory_ref) return;
    const feedback = form.closest(".work-card").querySelector("[data-import-start-feedback]");
    form.dataset.busy = "true";
    form.setAttribute("aria-busy", "true");
    const controls = Array.from(form.querySelectorAll("button, input"));
    const disabled = controls.map((control) => control.disabled);
    controls.forEach((control) => { control.disabled = true; });
    setText(feedback, "正在提交导入…");
    try {
      const inputs = { minecraft_version, export_id: form.dataset.selectedExportId || null, source_directory_ref, feature_workers };
      const saved = storedOperation("blockpedia.import");
      const retryId = form.dataset.retryRunId;
      const previous = retryId && retryId !== saved?.id ? { id: retryId } : saved;
      const run_id = previous?.id || "run_" + crypto.randomUUID().replaceAll("-", "");
      if (previous) {
        setText(feedback, "正在读取本次导入状态…");
        let existing = null;
        try {
          existing = await fetchJsonEnvelope(`/api/imports/${encodeURIComponent(run_id)}`);
          if (existing.run_id !== run_id) throw { code: "IMPORT_RESULT_INVALID" };
        } catch (error) {
          if (error.status !== 404) throw error;
        }
        let original = {};
        try { original = JSON.parse(previous.signature || "{}"); } catch (_) { /* Legacy identity: use the server snapshot. */ }
        const originalVersion = existing?.minecraft_version || original.minecraft_version;
        const originalExport = existing?.export_id || original.export_id;
        const originalWorkers = existing?.feature_workers ?? original.feature_workers ?? 1;
        if (originalWorkers !== feature_workers) {
          throw { code: "IMPORT_CONFLICT", message: `此运行已固定为 ${originalWorkers} 个特征计算进程。请恢复该值，或点击“开始新的导入操作”。` };
        }
        const sameSource = inputs.export_id && originalExport
          ? inputs.export_id === originalExport
          : source_directory_ref === original.source_directory_ref;
        if ((originalVersion && originalVersion !== minecraft_version)
          || ((originalExport || original.source_directory_ref) && !sameSource)) {
          throw { code: "IMPORT_SOURCE_SELECTION_CHANGED", message: "已有导入操作与所选导出或版本不一致（手动引用无法确认来源）。请选回原导出，或点击“开始新的导入操作”。" };
        }
        if (existing && ["pending", "running", "succeeded"].includes(existing.status)) {
          sessionStorage.setItem("blockpedia.import", JSON.stringify({ signature: JSON.stringify(inputs), id: run_id }));
          window.location.assign(`/imports/${encodeURIComponent(run_id)}`);
          return;
        }
      }
      sessionStorage.setItem("blockpedia.import", JSON.stringify({ signature: JSON.stringify(inputs), id: run_id }));
      const data = await postJsonEnvelope("/api/imports", { run_id, source_directory_ref, minecraft_version, feature_workers });
      if (data.run_id !== run_id) throw { code: "IMPORT_RESULT_INVALID" };
      window.location.assign(`/imports/${encodeURIComponent(run_id)}`);
    } catch (error) {
      setText(feedback, `${error.code || "IMPORT_RESPONSE_UNAVAILABLE"} · ${error.message || "未收到导入结果，请重试同一操作或查看最近导入。"}`);
    } finally {
      controls.forEach((control, index) => { control.disabled = disabled[index]; });
      form.dataset.busy = "false";
      form.removeAttribute("aria-busy");
    }
  };

  const submitRunCommand = async (form) => {
    const confirmation = form.dataset.confirm;
    if (confirmation && !window.confirm(confirmation)) return;
    const button = form.querySelector('button[type="submit"]');
    const feedback = form.closest("[data-run-fragment]")?.querySelector("[data-command-feedback]");
    const label = form.dataset.commandLabel || "命令";
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    setText(feedback, `${label}命令正在提交…`);
    try {
      const body = new URLSearchParams(new FormData(form));
      const response = await fetch(form.action, {
        method: "POST",
        headers: { Accept: "text/html" },
        body,
      });
      if (!response.ok) throw new Error(`HTTP_${response.status}`);
      setText(feedback, `${label}命令已提交，等待 Worker 状态快照。`);
      announce(`${label}命令已提交。`);
      const manager = streamManagers.get("run-panel");
      manager?.restartAfterCommand();
      if (!manager || !("EventSource" in window)) {
        window.setTimeout(() => window.location.reload(), 350);
      }
    } catch (_error) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      setText(feedback, `${label}命令未完成，请刷新状态后重试。`);
      announce(`${label}命令未完成。`);
    }
  };

  const readErrorFragment = (text, fallbackCode, fallbackMessage) => {
    if (typeof text !== "string") return { code: fallbackCode, message: fallbackMessage };
    const documentFragment = new DOMParser().parseFromString(text, "text/html");
    return {
      code: documentFragment.querySelector(".error-code")?.textContent?.trim() || fallbackCode,
      message: documentFragment.querySelector("h3")?.textContent?.trim() || fallbackMessage,
    };
  };

  const fetchJsonEnvelope = async (url, { signal } = {}) => {
    const target = safeSameOriginLocation(url);
    if (!target) throw { code: "INVALID_LOCAL_URL", message: "本地接口地址不合法。", status: 400 };
    const response = await fetch(target, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      credentials: "same-origin",
      signal,
    });
    const envelope = await response.json().catch(() => null);
    if (!response.ok || envelope?.ok === false) {
      throw {
        code: envelope?.error_code || `HTTP_${response.status}`,
        message: envelope?.message || "本地接口没有返回可用数据。",
        status: response.status,
      };
    }
    return envelope?.data || {};
  };

  const postJsonEnvelope = async (url, payload) => {
    const target = safeSameOriginLocation(url);
    if (!target) throw { code: "INVALID_LOCAL_URL", message: "本地接口地址不合法。", status: 400 };
    const response = await fetch(target, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      cache: "no-store",
      credentials: "same-origin",
    });
    const envelope = await response.json().catch(() => null);
    if (!response.ok || envelope?.ok === false) {
      throw {
        code: envelope?.error_code || `HTTP_${response.status}`,
        message: envelope?.message || "本地操作没有返回可用结果。",
        status: response.status,
      };
    }
    return envelope?.data || {};
  };

  const submitLocalForm = async (form) => {
    const target = safeSameOriginLocation(form.action);
    if (!target) throw { code: "INVALID_LOCAL_URL", message: "本地写入地址不合法。" };
    const response = await fetch(target, {
      method: "POST",
      headers: { Accept: "text/html", "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
      body: new URLSearchParams(new FormData(form)),
      cache: "no-store",
      credentials: "same-origin",
    });
    const text = await response.text();
    if (!response.ok) {
      throw readErrorFragment(text, `HTTP_${response.status}`, "本地操作未完成。");
    }
    return text;
  };

  const initializeProviderForm = (form) => {
    if (form.dataset.providerReady === "true") return;
    form.dataset.providerReady = "true";
    const adapter = form.querySelector("[data-adapter-select]");
    const adapterPolicy = form.querySelector("[data-adapter-policy]");
    const updateAdapterPolicy = () => {
      if (!adapter || !adapterPolicy) return;
      adapterPolicy.textContent = adapter.value === "openai_chat_completions"
        ? adapterPolicy.dataset.chatCopy
        : adapterPolicy.dataset.responsesCopy;
    };
    adapter?.addEventListener("change", updateAdapterPolicy);
    updateAdapterPolicy();

    const profileId = form.querySelector("[data-profile-id-input]");
    const secretReference = form.querySelector("[data-secret-reference]");
    const keyringOption = form.querySelector("[data-keyring-option]");
    if (!profileId || !secretReference || !keyringOption) return;
    const updateKeyringReference = () => {
      const clean = profileId.value.trim();
      const previous = keyringOption.value;
      keyringOption.value = `keyring:blockpedia/${clean}`;
      keyringOption.textContent = `OS Keyring · blockpedia/${clean || "profile"}`;
      if (secretReference.value === previous || secretReference.value.startsWith("keyring:blockpedia/")) {
        secretReference.value = keyringOption.value;
      }
    };
    profileId.addEventListener("input", updateKeyringReference);
    updateKeyringReference();
  };

  const initializeExplicitConfirmation = (form) => {
    if (form.dataset.confirmationReady === "true") return;
    form.dataset.confirmationReady = "true";
    const checkbox = form.querySelector("[data-explicit-confirmation-input]");
    const submit = form.querySelector("[data-explicit-confirmation-submit]");
    if (!checkbox || !submit) return;
    const sync = () => {
      submit.disabled = !checkbox.checked;
    };
    checkbox.addEventListener("change", sync);
    sync();
  };

  const applyProviderProbeView = (result) => {
    const card = result.closest("[data-provider-card]");
    if (!card) return;
    const passed = result.dataset.probeStatus === "verified";
    card.dataset.capabilityStatus = passed ? "verified" : "failed";
    const overall = card.querySelector("[data-provider-overall-status]");
    if (overall) {
      overall.textContent = passed ? "已验证，未启用" : "探测失败";
      overall.className = `status-badge status-badge--${passed ? "succeeded" : "failed"}`;
    }
    result.querySelectorAll("[data-probe-capability]").forEach((item) => {
      const capability = card.querySelector(`[data-capability="${item.dataset.probeCapability}"]`);
      if (!capability) return;
      const itemPassed = item.dataset.state === "passed";
      capability.dataset.state = itemPassed ? "passed" : "failed";
      setText(capability.querySelector("span"), itemPassed ? "✓" : "!");
      setText(capability.querySelector("small"), item.dataset.capabilityLabel || (itemPassed ? "已验证" : "未通过"));
    });
    const enable = card.querySelector("[data-provider-enable]");
    if (enable) enable.disabled = !passed;
    announce(passed ? "Provider 所选协议已验证，可以启用。" : "Provider 所选协议未通过，不能启用。");
  };

  const splitControlledValues = (value) => {
    const unique = new Set();
    String(value || "").split(/[\n,，]+/).forEach((item) => {
      const clean = item.trim();
      if (clean) unique.add(clean);
    });
    return Array.from(unique).slice(0, 64);
  };

  const selectedReviewDecision = (form) => form.querySelector('[name="decision"]:checked')?.value || "";

  const updateReviewEditor = (form) => {
    const decision = selectedReviewDecision(form);
    const editor = form.querySelector("[data-semantic-editor]");
    if (editor) editor.hidden = decision !== "edit_and_accept";
    const qualification = form.querySelector("[data-override-qualification]");
    const warningField = form.querySelector("[data-warning-field]");
    if (warningField) warningField.hidden = qualification?.value !== "conditional";
    const status = form.querySelector("[data-review-form-status]");
    const messages = {
      accept: "将接受并验证现有语义建议；仍需说明和证据。",
      edit_and_accept: "只会提交下方受控语义与资格字段。",
      skip: "跳过要求机器失败引用、说明与证据。",
      request_reexport: "将记录 Fabric exporter 重新导出请求；Studio 不修正机器事实。",
      request_exporter_rerender: "将记录 exporter 重渲染请求；Studio 不生成替代图片。",
      retry_ai: "将建立新的受审计 AI 尝试，不切换 profile 或模型。",
    };
    if (status && decision) {
      status.dataset.state = "ready";
      status.textContent = messages[decision] || "补充说明与证据后提交。";
    }
  };

  const serializeReviewOverride = (form) => {
    const hidden = form.querySelector("[data-review-override]");
    const decision = selectedReviewDecision(form);
    if (!hidden) return true;
    hidden.value = "";
    if (decision !== "edit_and_accept") return true;
    const operations = {};
    form.querySelectorAll("[data-override-field]").forEach((field) => {
      const value = field.value.trim();
      if (value) operations[field.dataset.overrideField] = value;
    });
    form.querySelectorAll("[data-override-list]").forEach((field) => {
      const values = splitControlledValues(field.value);
      if (values.length) operations[field.dataset.overrideList] = values;
    });
    const qualification = form.querySelector("[data-override-qualification]")?.value || "";
    const warnings = splitControlledValues(form.querySelector("[data-override-warnings]")?.value || "");
    const status = form.querySelector("[data-review-form-status]");
    if (qualification === "conditional" && !warnings.length) {
      status.dataset.state = "error";
      status.textContent = "conditional 资格至少需要一条警告。";
      form.querySelector("[data-override-warnings]")?.focus();
      return false;
    }
    if (!Object.keys(operations).length && !qualification) {
      status.dataset.state = "error";
      status.textContent = "编辑后接受至少需要一项语义修改或资格决定。";
      form.querySelector("[data-semantic-editor] input, [data-semantic-editor] textarea, [data-semantic-editor] select")?.focus();
      return false;
    }
    const override = { operations };
    if (qualification) {
      override.qualification = qualification;
      override.warnings = qualification === "conditional" ? warnings : [];
    }
    hidden.value = JSON.stringify(override);
    return true;
  };

  const initializeReviewForm = (form) => {
    if (form.dataset.reviewReady === "true") return;
    form.dataset.reviewReady = "true";
    form.querySelectorAll("[data-review-decision]").forEach((input) => {
      input.addEventListener("change", () => updateReviewEditor(form));
    });
    form.querySelector("[data-override-qualification]")?.addEventListener("change", () => updateReviewEditor(form));
    form.querySelectorAll("[data-evidence-preset]").forEach((button) => {
      button.addEventListener("click", () => {
        const evidence = form.querySelector('[name="evidence"]');
        if (!evidence) return;
        const values = splitControlledValues(evidence.value.replace(/\n/g, ","));
        if (!values.includes(button.dataset.evidencePreset)) values.push(button.dataset.evidencePreset);
        evidence.value = values.join("\n");
        evidence.focus({ preventScroll: true });
      });
    });
    updateReviewEditor(form);
  };

  const updateReviewContinue = () => {
    const continueForm = document.querySelector("[data-review-continue]");
    if (!continueForm) return;
    document.querySelectorAll("[data-review-list]").forEach((list) => {
      const count = list.querySelectorAll('[data-review-card][data-review-status="open"]').length;
      setText(list.closest(".review-queue")?.querySelector(".review-queue__header .count-badge"), String(count));
    });
    const openTasks = document.querySelectorAll('[data-review-card][data-review-status="open"]');
    continueForm.hidden = openTasks.length > 0;
    if (!openTasks.length) announce("全部审核任务已解决，可以继续运行。");
  };

  const setAIStatus = (control, state, message) => {
    const status = control.querySelector("[data-ai-status] .stream-state");
    if (!status) return;
    status.dataset.state = state;
    setText(status.querySelector("span"), message);
  };

  const showAIError = (control, error) => {
    const code = error?.code || "AI_PREVIEW_UNAVAILABLE";
    const message = error?.message || "发送前预览暂不可用。";
    setAIStatus(control, "reconnecting", `${code} · ${message}`);
    announce(`发送前预览未就绪：${code}。`);
  };

  const renderAIBatch = (control, batch) => {
    const runId = control.dataset.runId;
    const logicalKey = String(batch.logical_key || "");
    const imageLocation = safeSameOriginLocation(batch.image_url);
    if (!logicalKey || !batch.input_signature || !imageLocation || !imageLocation.pathname.startsWith(`/api/runs/${encodeURIComponent(runId)}/`)) {
      throw { code: "AI_BATCH_INPUT_INVALID", message: "批次预览缺少安全引用。" };
    }
    const preview = control.querySelector("[data-ai-preview]");
    const empty = control.querySelector("[data-ai-empty]");
    const configure = control.querySelector("[data-ai-configure]");
    preview.hidden = false;
    empty.hidden = true;
    configure.hidden = true;
    const image = control.querySelector("[data-ai-contact-sheet]");
    image.removeAttribute("src");
    image.src = imageLocation.pathname;
    setText(control.querySelector("[data-ai-image-url]"), imageLocation.pathname);
    setText(control.querySelector("[data-ai-prompt]"), String(batch.prompt || ""));
    setText(control.querySelector("[data-ai-logical-key]"), logicalKey);

    const tileMap = control.querySelector("[data-ai-tile-map]");
    tileMap.replaceChildren();
    const tiles = Array.isArray(batch.tiles) ? batch.tiles : [];
    tiles.forEach((tile) => {
      const item = document.createElement("li");
      const shortId = document.createElement("span");
      const variantId = document.createElement("code");
      shortId.textContent = String(tile.tile_id || "?");
      variantId.textContent = String(tile.variant_id || "unavailable");
      item.append(shortId, variantId);
      tileMap.append(item);
    });

    const approveForm = control.querySelector("[data-ai-approve]");
    const cancelForm = control.querySelector("[data-ai-cancel]");
    approveForm.action = `/ui/runs/${encodeURIComponent(runId)}/ai-batches/${encodeURIComponent(logicalKey)}/approve`;
    cancelForm.action = `/ui/runs/${encodeURIComponent(runId)}/ai-batches/${encodeURIComponent(logicalKey)}/cancel`;
    approveForm.querySelector("[data-ai-input-signature]").value = batch.input_signature;
    approveForm.querySelector("[data-ai-approve-submit]").disabled = tiles.length === 0 || !String(batch.prompt || "").trim();
    setAIStatus(control, "connected", `批次 ${logicalKey} 已在本地预览；尚未发送。`);
    announce(`AI 批次 ${logicalKey} 已显示，检查后才能批准。`);
  };

  const loadAIBatch = async (control) => {
    const preview = control.querySelector("[data-ai-preview]");
    const empty = control.querySelector("[data-ai-empty]");
    setAIStatus(control, "connecting", "正在读取下一批本地发送前预览");
    try {
      const batch = await fetchJsonEnvelope(`/api/runs/${encodeURIComponent(control.dataset.runId)}/ai-batches/next`);
      renderAIBatch(control, batch);
      return true;
    } catch (error) {
      preview.hidden = true;
      empty.hidden = false;
      if (["AI_BATCH_NOT_FOUND", "R2_PREREQUISITE_NOT_MET"].includes(error?.code) || error?.status === 404) {
        setAIStatus(control, "closed", "尚无待批准批次；可先配置此运行。 ");
        return false;
      }
      showAIError(control, error);
      return false;
    }
  };

  const initializeAIControl = async (control) => {
    if (control.dataset.aiReady === "true") return;
    control.dataset.aiReady = "true";
    const configureForm = control.querySelector("[data-ai-configure]");
    const configureButton = control.querySelector("[data-ai-configure-submit]");
    const range = control.querySelector("[data-range-input]");
    const output = control.querySelector("[data-range-output]");
    range?.addEventListener("input", () => setText(output, `${range.value}%`));
    try {
      const providerData = await fetchJsonEnvelope("/api/provider/profile");
      const profiles = Array.isArray(providerData.profiles) ? providerData.profiles : [];
      const active = profiles.find((profile) => profile.profile_id === providerData.active_profile_id && profile.enabled === true);
      if (active) {
        const credential = active.credential_status && typeof active.credential_status === "object" ? active.credential_status : {};
        const source = credential.source === "keyring" ? "OS Keyring" : ["env", "environment"].includes(credential.source) ? "环境变量" : "服务端解析";
        const activeAdapter = active.adapter === "openai_chat_completions"
          ? "openai_chat_completions"
          : (active.adapter === "openai_responses" || !active.adapter ? "openai_responses" : "未识别 adapter");
        setText(control.querySelector("[data-ai-profile-id]"), active.profile_id);
        setText(control.querySelector("[data-ai-adapter]"), activeAdapter);
        setText(control.querySelector("[data-ai-model-id]"), active.model_id);
        setText(control.querySelector("[data-ai-endpoint]"), active.base_url_stable_id || active.base_url || "未报告");
        setText(control.querySelector("[data-ai-prompt-version]"), active.prompt_version || "prompt.v1");
        const frozenConcurrency = Number(control.dataset.offlineConcurrency);
        const profileConcurrency = Number(active.stages?.offline_annotation?.concurrency || 1);
        const visibleConcurrency = Number.isInteger(frozenConcurrency) && frozenConcurrency >= 1 && frozenConcurrency <= 5
          ? `${frozenConcurrency} · 已为此运行冻结`
          : `${Number.isInteger(profileConcurrency) && profileConcurrency >= 1 && profileConcurrency <= 5 ? profileConcurrency : 1} · 配置运行时冻结`;
        setText(control.querySelector("[data-ai-concurrency]"), visibleConcurrency);
        setText(control.querySelector("[data-ai-credential]"), `${source} · ${credential.masked || "已配置"}`);
        configureForm.querySelector("[data-ai-profile-input]").value = active.profile_id;
        const preferredBatch = Number(active.stages?.offline_annotation?.batch_size || 12);
        const batchSelect = configureForm.querySelector('[name="batch_size"]');
        if ([8, 12, 16].includes(preferredBatch)) batchSelect.value = String(preferredBatch);
        control.dataset.profileReady = credential.configured ? "true" : "false";
        configureButton.disabled = true;
      } else {
        setText(control.querySelector("[data-ai-profile-id]"), "无 active profile");
        setText(control.querySelector("[data-ai-adapter]"), "未配置");
        setText(control.querySelector("[data-ai-model-id]"), "先到 Provider 页面启用");
        setText(control.querySelector("[data-ai-endpoint]"), "未配置");
        setText(control.querySelector("[data-ai-prompt-version]"), "未配置");
        setText(control.querySelector("[data-ai-concurrency]"), "未配置");
        setText(control.querySelector("[data-ai-credential]"), "不可用");
        control.dataset.profileReady = "false";
        configureButton.disabled = true;
      }
    } catch (error) {
      configureButton.disabled = true;
      showAIError(control, error);
    }
    const hasBatch = await loadAIBatch(control);
    const atConfigureBoundary = control.dataset.boundaryEvent === "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING"
      || (control.dataset.currentStage === "AI_ANNOTATE" && control.dataset.runStatus === "paused");
    configureButton.disabled = hasBatch || control.dataset.profileReady !== "true" || !atConfigureBoundary;
  };

  const submitAIConfigure = async (form) => {
    const control = form.closest("[data-ai-control]");
    const button = form.querySelector('[type="submit"]');
    if (!control || !form.reportValidity() || !form.querySelector('[name="profile_id"]')?.value) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    setAIStatus(control, "connecting", "正在建立本地批次；此步骤不会发送给 provider");
    try {
      await submitLocalForm(form);
      streamManagers.get("run-panel")?.restartAfterCommand();
      await loadAIBatch(control);
    } catch (error) {
      showAIError(control, error);
      button.disabled = false;
    } finally {
      button.removeAttribute("aria-busy");
    }
  };

  const submitAIBatchAction = async (form, action) => {
    const control = form.closest("[data-ai-control]");
    const button = form.querySelector('[type="submit"]');
    if (!control) return;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    setAIStatus(control, "connecting", action === "approve" ? "正在记录批准并等待 Worker" : "正在取消本批；不会发送");
    try {
      await submitLocalForm(form);
      streamManagers.get("run-panel")?.restartAfterCommand();
      if (action === "approve") {
        await loadAIBatch(control);
      } else {
        control.querySelector("[data-ai-preview]").hidden = true;
        control.querySelector("[data-ai-empty]").hidden = false;
        setAIStatus(control, "closed", "本批已取消且没有发送；相关条目已进入人工审核。 ");
        announce("AI 批次已取消，没有发送；相关条目已进入人工审核。 ");
      }
    } catch (error) {
      showAIError(control, error);
      button.disabled = false;
    } finally {
      button.removeAttribute("aria-busy");
    }
  };

  const aiQueueErrorMessages = {
    AI_BATCH_PLAN_CONFLICT: "剩余批次已经变化，请取消后重新预览。",
    AI_RETRY_WAVE_CONFLICT: "可重试批次已经变化，请取消后重新预览。",
    PROVIDER_RETRY_NOT_ELIGIBLE: "此批次当前不再符合重试条件，请刷新状态。",
    RUN_STATE_CONFLICT: "运行状态已经变化，请刷新后重试。",
    AI_BATCH_INPUT_INVALID: "批次输入当前无法形成安全计划，请检查审核队列。",
    RUN_NOT_FOUND: "当前运行不存在或已不可用。",
    INVALID_INPUT: "提交内容不完整，请重新预览后再试。",
  };

  const safeAIQueueCode = (value, fallback = "AI_QUEUE_COMMAND_FAILED") => {
    const code = String(value || "");
    return /^[A-Z][A-Z0-9_]{1,127}$/.test(code) ? code : fallback;
  };

  const safeAIQueueText = (value, fallback, maxLength = 240) => {
    if (typeof value !== "string") return fallback;
    const text = value.trim();
    if (!text || text.length > maxLength || /[\u0000-\u001f\u007f]/.test(text)) return fallback;
    return text;
  };

  const safeAIQueueId = (value) => {
    const identifier = String(value || "");
    return /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/.test(identifier) ? identifier : null;
  };

  const safeAIQueueHash = (value) => {
    const hash = String(value || "");
    return /^sha256:[0-9a-f]{64}$/.test(hash) ? hash : null;
  };

  const abbreviateAIQueueHash = (hash) => `${hash.slice(0, 17)}…${hash.slice(-8)}`;

  const safeAIQueueCount = (value) => {
    if (typeof value !== "number" && !(typeof value === "string" && /^\d+$/.test(value))) return null;
    const count = Number(value);
    return Number.isInteger(count) && count >= 0 && count <= 10000 ? count : null;
  };

  const safeAIPlanRoute = (value, runId) => {
    const target = safeSameOriginLocation(value);
    if (!target || target.search || target.hash) return null;
    const prefix = `/api/runs/${encodeURIComponent(runId)}/`;
    return target.pathname.startsWith(prefix) ? target.pathname : null;
  };

  const safeAIPlanMultiline = (value, maxLength = 50000) => {
    if (typeof value !== "string" || !value || value.length > maxLength) return null;
    return /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value) ? null : value;
  };

  const safeAIPlanMetadataText = (value) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    try {
      const text = JSON.stringify(value, null, 2);
      return text.length <= 200000 ? text : null;
    } catch (_error) {
      return null;
    }
  };

  const normalizeAIPlanJobs = (value, expectedCount, runId) => {
    if (!Array.isArray(value) || value.length !== expectedCount) return null;
    const identifiers = new Set();
    const jobs = [];
    for (const item of value) {
      if (!item || typeof item !== "object") return null;
      const jobId = safeAIQueueId(item.job_id);
      const logicalKey = safeAIQueueText(item.logical_key, null, 256);
      const signature = safeAIQueueHash(item.input_signature);
      const previewUrl = safeAIPlanRoute(item.preview_url, runId);
      const imageUrl = safeAIPlanRoute(item.image_url, runId);
      if (!jobId || identifiers.has(jobId) || !logicalKey || !signature || !previewUrl || !imageUrl) return null;
      identifiers.add(jobId);
      jobs.push({ jobId, logicalKey, signature, previewUrl, imageUrl });
    }
    return jobs;
  };

  const showAIQueueFeedback = (panel, state, message, code = "") => {
    const feedback = panel?.querySelector("[data-ai-queue-feedback]");
    if (!feedback) return;
    feedback.hidden = !message;
    feedback.dataset.state = state;
    setText(feedback, code ? `${code} · ${message}` : message);
  };

  const showAIQueueError = (panel, error, fallback) => {
    const code = safeAIQueueCode(error?.code);
    showAIQueueFeedback(panel, "error", aiQueueErrorMessages[code] || fallback, code);
    announce(`批次操作未完成：${code}。`);
  };

  const setAIQueueControlsDisabled = (panel, disabled) => {
    panel?.closest("[data-run-fragment]")?.querySelectorAll("[data-ai-queue-preview], [data-ai-job-retry]").forEach((button) => {
      button.disabled = disabled;
    });
  };

  const pauseAIQueueStream = () => {
    streamManagers.get("run-panel")?.pauseForDecision();
  };

  const resumeAIQueueStream = () => {
    streamManagers.get("run-panel")?.resumeAfterDecision();
  };

  const setAIQueueFact = (confirmation, name, value) => {
    const row = confirmation.querySelector(`[data-ai-confirm-field="${name}"]`);
    const target = row?.querySelector(`[data-ai-confirm-value="${name}"]`);
    if (!row || !target) return;
    row.hidden = value === null || value === undefined || value === "";
    setText(target, row.hidden ? "" : String(value));
  };

  const resetAIQueueFacts = (confirmation) => {
    confirmation.querySelectorAll("[data-ai-confirm-field]").forEach((row) => {
      row.hidden = true;
      setText(row.querySelector("[data-ai-confirm-value]"), "");
    });
  };

  const clearAIPlanInspector = (confirmation) => {
    if (!confirmation) return;
    const state = aiPlanInspectorStates.get(confirmation);
    state?.controller?.abort();
    const inspector = confirmation.querySelector("[data-ai-plan-inspector]");
    const image = confirmation.querySelector("[data-ai-plan-image]");
    image?.removeAttribute("src");
    if (image) image.alt = "";
    state?.cache?.forEach((preview) => {
      if (preview.objectUrl) URL.revokeObjectURL(preview.objectUrl);
    });
    state?.cache?.clear();
    state?.jobs?.clear();
    state?.buttons?.clear();
    aiPlanInspectorStates.delete(confirmation);
    confirmation.querySelector("[data-ai-plan-job-list]")?.replaceChildren();
    setText(confirmation.querySelector("[data-ai-plan-cache-count]"), "已读取 0 / 0");
    setText(confirmation.querySelector("[data-ai-plan-preview-heading]"), "选择一个批次");
    setText(confirmation.querySelector("[data-ai-plan-preview-signature]"), "");
    const previewStatus = confirmation.querySelector("[data-ai-plan-preview-status]");
    if (previewStatus) {
      previewStatus.dataset.state = "idle";
      previewStatus.textContent = "从左侧列表打开任意批次，查看实际安全预览。";
    }
    const content = confirmation.querySelector("[data-ai-plan-preview-content]");
    if (content) content.hidden = true;
    const back = confirmation.querySelector("[data-ai-plan-preview-back]");
    if (back) back.hidden = true;
    setText(confirmation.querySelector("[data-ai-plan-prompt]"), "");
    setText(confirmation.querySelector("[data-ai-plan-metadata]"), "");
    confirmation.querySelector("[data-ai-plan-tiles]")?.replaceChildren();
    if (inspector) inspector.hidden = true;
  };

  const updateAIPlanCacheCount = (confirmation, state) => {
    setText(confirmation.querySelector("[data-ai-plan-cache-count]"), `已读取 ${state.cache.size} / ${state.jobs.size}`);
  };

  const setAIPlanJobButtonState = (state, jobId, previewState, label) => {
    const button = state.buttons.get(jobId);
    if (!button) return;
    button.dataset.previewState = previewState;
    setText(button.querySelector(".ai-plan-job-button__state"), label);
  };

  const setupAIPlanInspector = (confirmation, jobs) => {
    const inspector = confirmation.querySelector("[data-ai-plan-inspector]");
    const list = confirmation.querySelector("[data-ai-plan-job-list]");
    if (!inspector || !list) throw { code: "AI_QUEUE_PREVIEW_INVALID" };
    const state = {
      jobs: new Map(jobs.map((job) => [job.jobId, job])),
      buttons: new Map(),
      cache: new Map(),
      activeJobId: null,
      controller: null,
    };
    aiPlanInspectorStates.set(confirmation, state);
    list.replaceChildren();
    jobs.forEach((job, index) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      const number = document.createElement("span");
      const identity = document.createElement("span");
      const label = document.createElement("b");
      const signature = document.createElement("code");
      const previewState = document.createElement("small");
      button.type = "button";
      button.className = "ai-plan-job-button";
      button.dataset.aiPlanJob = "true";
      button.dataset.jobId = job.jobId;
      button.dataset.previewState = "idle";
      button.setAttribute("aria-controls", "ai-plan-preview-detail");
      button.setAttribute("aria-expanded", "false");
      button.setAttribute("aria-label", `查看批次预览 ${index + 1}：${job.logicalKey}`);
      button.tabIndex = index === 0 ? 0 : -1;
      number.className = "ai-plan-job-button__number";
      number.setAttribute("aria-hidden", "true");
      number.textContent = String(index + 1).padStart(3, "0");
      identity.className = "ai-plan-job-button__identity";
      label.textContent = job.logicalKey;
      signature.textContent = abbreviateAIQueueHash(job.signature);
      identity.append(label, signature);
      previewState.className = "ai-plan-job-button__state";
      previewState.textContent = "查看预览";
      button.append(number, identity, previewState);
      item.append(button);
      list.append(item);
      state.buttons.set(job.jobId, button);
    });
    inspector.hidden = false;
    updateAIPlanCacheCount(confirmation, state);
  };

  const normalizeAIPlanPreview = (data, job, runId) => {
    if (!data || typeof data !== "object") return null;
    const responseJobId = data.job_id ? safeAIQueueId(data.job_id) : job.jobId;
    const logicalKey = safeAIQueueText(data.logical_key, null, 256);
    const signature = safeAIQueueHash(data.input_signature);
    const imageUrl = safeAIPlanRoute(data.image_url, runId);
    const prompt = safeAIPlanMultiline(data.prompt);
    const metadataText = safeAIPlanMetadataText(data.machine_metadata);
    if (responseJobId !== job.jobId || logicalKey !== job.logicalKey || signature !== job.signature || imageUrl !== job.imageUrl || !prompt || !metadataText) return null;
    if (!Array.isArray(data.tiles) || data.tiles.length === 0 || data.tiles.length > 256) return null;
    const tiles = [];
    for (const item of data.tiles) {
      if (!item || typeof item !== "object") return null;
      const tileId = safeAIQueueText(item.tile_id, null, 128);
      const variantId = safeAIQueueText(item.variant_id, null, 256);
      if (!tileId || !variantId) return null;
      tiles.push({ tileId, variantId });
    }
    return { logicalKey, signature, imageUrl, prompt, metadataText, tiles, objectUrl: null };
  };

  const fetchAIPlanImage = async (imageUrl, signal) => {
    const target = safeSameOriginLocation(imageUrl);
    if (!target) throw { code: "AI_PLAN_IMAGE_INVALID" };
    const response = await fetch(target, {
      headers: { Accept: "image/png" },
      cache: "no-store",
      credentials: "same-origin",
      signal,
    });
    if (!response.ok) throw { code: `HTTP_${response.status}` };
    const blob = await response.blob();
    if (blob.type !== "image/png" || blob.size <= 0 || blob.size > 16 * 1024 * 1024) throw { code: "AI_PLAN_IMAGE_INVALID" };
    const objectUrl = URL.createObjectURL(blob);
    if (signal.aborted) {
      URL.revokeObjectURL(objectUrl);
      throw { name: "AbortError" };
    }
    return objectUrl;
  };

  const renderAIPlanPreview = (confirmation, state, preview, fromCache) => {
    const detail = confirmation.querySelector("[data-ai-plan-preview-detail]");
    const content = confirmation.querySelector("[data-ai-plan-preview-content]");
    const status = confirmation.querySelector("[data-ai-plan-preview-status]");
    const image = confirmation.querySelector("[data-ai-plan-image]");
    const tileList = confirmation.querySelector("[data-ai-plan-tiles]");
    if (!detail || !content || !status || !image || !tileList) return;
    state.buttons.forEach((button, jobId) => {
      const selected = jobId === state.activeJobId;
      button.setAttribute("aria-expanded", selected ? "true" : "false");
      button.tabIndex = selected ? 0 : -1;
    });
    setText(confirmation.querySelector("[data-ai-plan-preview-heading]"), preview.logicalKey);
    setText(confirmation.querySelector("[data-ai-plan-preview-signature]"), preview.signature);
    status.dataset.state = "ready";
    status.textContent = fromCache ? "已从当前确认会话的内存缓存读取。" : "安全预览与联系表已读取。";
    image.src = preview.objectUrl;
    image.alt = `批次 ${preview.logicalKey} 的本地联系表`;
    setText(confirmation.querySelector("[data-ai-plan-prompt]"), preview.prompt);
    setText(confirmation.querySelector("[data-ai-plan-metadata]"), preview.metadataText);
    tileList.replaceChildren();
    preview.tiles.forEach((tile) => {
      const item = document.createElement("li");
      const tileId = document.createElement("span");
      const variantId = document.createElement("code");
      tileId.textContent = tile.tileId;
      variantId.textContent = tile.variantId;
      item.append(tileId, variantId);
      tileList.append(item);
    });
    content.hidden = false;
    const back = confirmation.querySelector("[data-ai-plan-preview-back]");
    if (back) back.hidden = false;
    detail.focus({ preventScroll: true });
    detail.scrollIntoView({ behavior: reducedMotion.matches ? "auto" : "smooth", block: "nearest" });
  };

  const showAIPlanPreviewError = (confirmation, state, job, code) => {
    const detail = confirmation.querySelector("[data-ai-plan-preview-detail]");
    const content = confirmation.querySelector("[data-ai-plan-preview-content]");
    const status = confirmation.querySelector("[data-ai-plan-preview-status]");
    confirmation.querySelector("[data-ai-plan-image]")?.removeAttribute("src");
    if (content) content.hidden = true;
    setText(confirmation.querySelector("[data-ai-plan-preview-heading]"), job.logicalKey);
    setText(confirmation.querySelector("[data-ai-plan-preview-signature]"), job.signature);
    if (status) {
      status.dataset.state = "error";
      status.textContent = `${safeAIQueueCode(code, "AI_PLAN_PREVIEW_UNAVAILABLE")} · 此批次预览未读取，请重试或检查运行状态。`;
    }
    const back = confirmation.querySelector("[data-ai-plan-preview-back]");
    if (back) back.hidden = false;
    setAIPlanJobButtonState(state, job.jobId, "error", "读取失败");
    detail?.focus({ preventScroll: true });
  };

  const loadAIPlanJobPreview = async (button) => {
    if (button.disabled) return;
    const confirmation = button.closest("[data-ai-queue-confirmation]");
    const state = aiPlanInspectorStates.get(confirmation);
    const job = state?.jobs.get(button.dataset.jobId);
    const runId = safeAIQueueId(confirmation?.closest("[data-ai-queue-actions]")?.dataset.runId);
    if (!confirmation || !state || !job || !runId) return;
    const previousJobId = state.activeJobId;
    state.controller?.abort();
    state.controller = null;
    if (previousJobId && !state.cache.has(previousJobId) && state.buttons.get(previousJobId)?.dataset.previewState === "loading") {
      setAIPlanJobButtonState(state, previousJobId, "idle", "查看预览");
    }
    state.activeJobId = job.jobId;
    state.buttons.forEach((item, jobId) => {
      const selected = jobId === job.jobId;
      item.setAttribute("aria-expanded", selected ? "true" : "false");
      item.tabIndex = selected ? 0 : -1;
    });
    const cached = state.cache.get(job.jobId);
    if (cached) {
      renderAIPlanPreview(confirmation, state, cached, true);
      return;
    }
    const controller = new AbortController();
    state.controller = controller;
    const detail = confirmation.querySelector("[data-ai-plan-preview-detail]");
    const content = confirmation.querySelector("[data-ai-plan-preview-content]");
    const status = confirmation.querySelector("[data-ai-plan-preview-status]");
    confirmation.querySelector("[data-ai-plan-image]")?.removeAttribute("src");
    if (content) content.hidden = true;
    setText(confirmation.querySelector("[data-ai-plan-preview-heading]"), job.logicalKey);
    setText(confirmation.querySelector("[data-ai-plan-preview-signature]"), job.signature);
    if (status) {
      status.dataset.state = "loading";
      status.textContent = "正在读取此批次的安全文本、机器 metadata 与本地联系表…";
    }
    const back = confirmation.querySelector("[data-ai-plan-preview-back]");
    if (back) back.hidden = false;
    setAIPlanJobButtonState(state, job.jobId, "loading", "正在读取");
    detail?.focus({ preventScroll: true });
    try {
      const data = await fetchJsonEnvelope(job.previewUrl, { signal: controller.signal });
      const preview = normalizeAIPlanPreview(data, job, runId);
      if (!preview) throw { code: "AI_PLAN_PREVIEW_CHANGED" };
      preview.objectUrl = await fetchAIPlanImage(preview.imageUrl, controller.signal);
      if (controller.signal.aborted || state.controller !== controller) {
        URL.revokeObjectURL(preview.objectUrl);
        return;
      }
      state.cache.set(job.jobId, preview);
      state.controller = null;
      setAIPlanJobButtonState(state, job.jobId, "cached", "已读取");
      updateAIPlanCacheCount(confirmation, state);
      renderAIPlanPreview(confirmation, state, preview, false);
    } catch (error) {
      if (error?.name === "AbortError" || state.controller !== controller) return;
      state.controller = null;
      showAIPlanPreviewError(confirmation, state, job, error?.code);
      announce(`批次 ${job.logicalKey} 的预览未读取。`);
    }
  };

  const focusActiveAIPlanJob = (confirmation) => {
    const state = aiPlanInspectorStates.get(confirmation);
    const button = state?.buttons.get(state.activeJobId) || state?.buttons.values().next().value;
    button?.focus({ preventScroll: true });
    button?.scrollIntoView({ behavior: reducedMotion.matches ? "auto" : "smooth", block: "nearest" });
  };

  const openAIQueueConfirmation = (panel, trigger, spec) => {
    const confirmation = panel.querySelector("[data-ai-queue-confirmation]");
    if (!confirmation) return;
    clearAIPlanInspector(confirmation);
    resetAIQueueFacts(confirmation);
    confirmation.dataset.commandKind = spec.kind;
    confirmation.dataset.commandHash = spec.hash || "";
    confirmation.dataset.commandJobId = spec.jobId || "";
    setText(confirmation.querySelector("[data-ai-confirm-title]"), spec.title);
    setText(confirmation.querySelector("[data-ai-confirm-copy]"), spec.copy);
    setText(confirmation.querySelector("[data-ai-confirm-hash-label]"), spec.hashLabel || "确认哈希");
    setAIQueueFact(confirmation, "model", spec.model);
    setAIQueueFact(confirmation, "adapter", spec.adapter);
    setAIQueueFact(confirmation, "concurrency", spec.concurrency ? `${spec.concurrency} · 已为此运行冻结` : null);
    setAIQueueFact(confirmation, "count", spec.count);
    setAIQueueFact(confirmation, "hash", spec.hash ? abbreviateAIQueueHash(spec.hash) : null);
    setAIQueueFact(confirmation, "batch", spec.batch);
    setAIQueueFact(confirmation, "error", spec.errorCode);
    const submit = confirmation.querySelector("[data-ai-confirm-submit]");
    setText(submit, spec.submitLabel);
    confirmation.dataset.submitLabel = spec.submitLabel;
    if (spec.kind === "plan") setupAIPlanInspector(confirmation, spec.planJobs || []);
    confirmation.hidden = false;
    confirmation.removeAttribute("aria-busy");
    trigger.setAttribute("aria-expanded", "true");
    aiQueueConfirmationTriggers.set(confirmation, trigger);
    showAIQueueFeedback(panel, "neutral", "");
    confirmation.focus({ preventScroll: true });
    confirmation.scrollIntoView({ behavior: reducedMotion.matches ? "auto" : "smooth", block: "nearest" });
  };

  const closeAIQueueConfirmation = (confirmation, { restoreFocus = true } = {}) => {
    if (!confirmation || confirmation.getAttribute("aria-busy") === "true") return;
    const panel = confirmation.closest("[data-ai-queue-actions]");
    const trigger = aiQueueConfirmationTriggers.get(confirmation);
    clearAIPlanInspector(confirmation);
    resetAIQueueFacts(confirmation);
    setText(confirmation.querySelector("[data-ai-confirm-copy]"), "");
    confirmation.hidden = true;
    delete confirmation.dataset.commandKind;
    delete confirmation.dataset.commandHash;
    delete confirmation.dataset.commandJobId;
    delete confirmation.dataset.submitLabel;
    trigger?.setAttribute("aria-expanded", "false");
    aiQueueConfirmationTriggers.delete(confirmation);
    setAIQueueControlsDisabled(panel, false);
    showAIQueueFeedback(panel, "neutral", "已取消，没有提交批次命令。 ");
    resumeAIQueueStream();
    if (restoreFocus && trigger) {
      trigger.focus({ preventScroll: true });
      trigger.scrollIntoView({ behavior: reducedMotion.matches ? "auto" : "smooth", block: "nearest" });
    }
  };

  const previewAIQueueCommand = async (button) => {
    if (button.disabled) return;
    const panel = button.closest("[data-ai-queue-actions]");
    const kind = button.dataset.aiQueuePreview;
    if (!panel || !["plan", "wave"].includes(kind)) return;
    const previewUrl = kind === "plan" ? panel.dataset.planPreviewUrl : panel.dataset.wavePreviewUrl;
    pauseAIQueueStream();
    setAIQueueControlsDisabled(panel, true);
    button.setAttribute("aria-busy", "true");
    showAIQueueFeedback(panel, "loading", kind === "plan" ? "正在读取剩余批次计划…" : "正在读取可重试批次…");
    let opened = false;
    try {
      const data = await fetchJsonEnvelope(previewUrl);
      const count = safeAIQueueCount(data.count ?? data.batch_count);
      const hash = safeAIQueueHash(kind === "plan" ? data.plan_hash : data.wave_hash);
      if (count === null || !hash) throw { code: "AI_QUEUE_PREVIEW_INVALID" };
      if (count === 0) {
        showAIQueueFeedback(panel, "neutral", kind === "plan" ? "当前没有待批准的 AI 批次。" : "当前没有可重试的失败批次。");
        return;
      }
      if (kind === "plan") {
        const adapter = ["openai_responses", "openai_chat_completions"].includes(data.adapter) ? data.adapter : null;
        const model = safeAIQueueText(data.requested_model_id || data.model_id, null, 200);
        const concurrency = Number(data.offline_annotation_concurrency);
        const runId = safeAIQueueId(panel.dataset.runId);
        const planJobs = runId ? normalizeAIPlanJobs(data.jobs, count, runId) : null;
        if (!adapter || !model || !Number.isInteger(concurrency) || concurrency < 1 || concurrency > 5 || !planJobs) throw { code: "AI_QUEUE_PREVIEW_INVALID" };
        openAIQueueConfirmation(panel, button, {
          kind,
          hash,
          count,
          adapter,
          model,
          concurrency,
          planJobs,
          title: "确认自动处理剩余批次",
          hashLabel: "不可变计划哈希",
          copy: "确认后会向所选 provider 提交这些批次，并使用此运行已冻结的离线并发度。更改 active profile 不会改写此计划；每个逻辑请求仍遵守原有自动重试预算。普通失败会记录后继续，认证或配置等致命错误会停止。",
          submitLabel: "确认并按冻结并发度处理",
        });
      } else {
        openAIQueueConfirmation(panel, button, {
          kind,
          hash,
          count,
          title: "确认批量重试失败批次",
          hashLabel: "重试波次哈希",
          copy: "确认后为每个符合条件的叶子失败项建立一个确定性的重试子任务；原任务、错误码和审核证据保持不变。重复提交按同一波次幂等处理。",
          submitLabel: "确认建立重试批次",
        });
      }
      opened = true;
    } catch (error) {
      showAIQueueError(panel, error, "无法读取安全的批次预览，请刷新状态后重试。");
    } finally {
      button.removeAttribute("aria-busy");
      if (!opened) {
        setAIQueueControlsDisabled(panel, false);
        resumeAIQueueStream();
      }
    }
  };

  const previewSingleJobRetry = (button) => {
    if (button.disabled) return;
    const runFragment = button.closest("[data-run-fragment]");
    const panel = runFragment?.querySelector("[data-ai-queue-actions]");
    const jobId = safeAIQueueId(button.dataset.jobId);
    const batch = safeAIQueueText(button.dataset.logicalKey, null, 256);
    const errorCode = safeAIQueueCode(button.dataset.errorCode, "PROVIDER_RETRY_NOT_ELIGIBLE");
    if (!panel || !jobId || !batch) return;
    pauseAIQueueStream();
    setAIQueueControlsDisabled(panel, true);
    openAIQueueConfirmation(panel, button, {
      kind: "job",
      jobId,
      batch,
      errorCode,
      title: "确认重试此批次",
      copy: "确认后建立一个确定性的重试子任务；原批次、错误码和审核证据保持不变。重复提交可以幂等返回已有结果。",
      submitLabel: "确认重试此批次",
    });
  };

  const refreshRunAfterAIQueueCommand = (panel) => {
    const manager = streamManagers.get("run-panel");
    if (manager && "EventSource" in window) {
      manager.restartAfterCommand();
      window.setTimeout(() => {
        if (document.contains(panel)) window.location.reload();
      }, 2400);
      return;
    }
    window.setTimeout(() => window.location.reload(), 350);
  };

  const submitAIQueueCommand = async (confirmation) => {
    if (!confirmation || confirmation.getAttribute("aria-busy") === "true") return;
    const panel = confirmation.closest("[data-ai-queue-actions]");
    const submit = confirmation.querySelector("[data-ai-confirm-submit]");
    const cancel = confirmation.querySelector("[data-ai-confirm-cancel]");
    const kind = confirmation.dataset.commandKind;
    const runId = safeAIQueueId(panel?.dataset.runId);
    if (!panel || !submit || !cancel || !runId || !["plan", "wave", "job"].includes(kind)) return;
    let url;
    let payload;
    if (kind === "plan") {
      url = panel.dataset.planApproveUrl;
      payload = { plan_hash: confirmation.dataset.commandHash };
    } else if (kind === "wave") {
      url = panel.dataset.waveApproveUrl;
      payload = { wave_hash: confirmation.dataset.commandHash };
    } else {
      const jobId = safeAIQueueId(confirmation.dataset.commandJobId);
      if (!jobId) return;
      url = `/api/runs/${encodeURIComponent(runId)}/ai-batches/jobs/${encodeURIComponent(jobId)}/retry`;
      payload = { confirm: true };
    }
    const inspectorState = aiPlanInspectorStates.get(confirmation);
    if (inspectorState?.controller) {
      inspectorState.controller.abort();
      inspectorState.controller = null;
      if (inspectorState.activeJobId && !inspectorState.cache.has(inspectorState.activeJobId)) {
        setAIPlanJobButtonState(inspectorState, inspectorState.activeJobId, "idle", "查看预览");
      }
      const previewStatus = confirmation.querySelector("[data-ai-plan-preview-status]");
      if (previewStatus) {
        previewStatus.dataset.state = "idle";
        previewStatus.textContent = "批次预览读取已暂停；如果命令未提交，可以重新打开此批次。";
      }
    }
    confirmation.setAttribute("aria-busy", "true");
    submit.disabled = true;
    cancel.disabled = true;
    setText(submit, "正在提交…");
    showAIQueueFeedback(panel, "loading", "正在提交已确认的批次命令…");
    try {
      await postJsonEnvelope(url, payload);
      const successMessage = kind === "plan"
        ? "剩余批次计划已批准，等待按顺序处理。"
        : kind === "wave"
          ? "重试批次已建立，原始证据保持不变。"
          : "重试子任务已建立，原批次记录保持不变。";
      clearAIPlanInspector(confirmation);
      resetAIQueueFacts(confirmation);
      setText(confirmation.querySelector("[data-ai-confirm-copy]"), "");
      confirmation.hidden = true;
      delete confirmation.dataset.commandKind;
      delete confirmation.dataset.commandHash;
      delete confirmation.dataset.commandJobId;
      delete confirmation.dataset.submitLabel;
      const trigger = aiQueueConfirmationTriggers.get(confirmation);
      trigger?.setAttribute("aria-expanded", "false");
      aiQueueConfirmationTriggers.delete(confirmation);
      showAIQueueFeedback(panel, "success", successMessage);
      announce(successMessage);
      refreshRunAfterAIQueueCommand(panel);
    } catch (error) {
      confirmation.removeAttribute("aria-busy");
      submit.disabled = false;
      cancel.disabled = false;
      setText(submit, confirmation.dataset.submitLabel || "确认并提交");
      showAIQueueError(panel, error, "命令未提交，请按错误码处理后重试或取消。 ");
    }
  };

  const initializeReleaseCandidate = (panel) => {
    if (panel.dataset.candidateReady) return;
    panel.dataset.candidateReady = "true";
    const button = panel.querySelector("[data-candidate-build]");
    const fresh = panel.querySelector("[data-new-build]");
    const feedback = panel.querySelector("[data-build-feedback]");
    const key = `blockpedia.build.${panel.dataset.runId}`;
    const inputs = { run_id: panel.dataset.runId, minecraft_version: panel.dataset.minecraftVersion };
    const saved = storedOperation(key);
    if (saved?.signature === JSON.stringify(inputs)) {
      button.disabled = false;
      button.textContent = "重试本次构建并读取结果";
      setText(feedback, saved.release_id
        ? `候选已构建：${saved.release_id}。可读取本次结果，或前往发布与回滚。`
        : "已恢复本次构建标识，重试将读取或继续同一次构建。");
    }
    fresh.addEventListener("click", () => {
      sessionStorage.removeItem(key);
      button.disabled = false;
      button.textContent = "构建候选";
      setText(feedback, "已开始新的构建操作。");
    });
    button.addEventListener("click", async () => {
      if (button.disabled) return;
      button.disabled = fresh.disabled = true;
      panel.setAttribute("aria-busy", "true");
      setText(feedback, "正在构建候选…");
      try {
        const release_build_id = operationId(key, "build_", inputs);
        const data = await postJsonEnvelope("/api/releases/build", { ...inputs, release_build_id });
        if (data.release_build_id !== release_build_id || data.status !== "built") throw { code: "RELEASE_BUILD_RESULT_INVALID" };
        sessionStorage.setItem(key, JSON.stringify({ signature: JSON.stringify(inputs), id: release_build_id, release_id: data.release_id }));
        setText(feedback, `候选已构建：${data.release_id}。请前往发布与回滚确认发布。`);
        button.textContent = "查看本次构建结果";
      } catch (error) {
        setText(feedback, `${error.code || "RELEASE_BUILD_RESPONSE_UNAVAILABLE"} · ${error.message || "未收到构建结果，重试将复用本次构建标识。"}`);
      } finally {
        button.disabled = fresh.disabled = false;
        panel.removeAttribute("aria-busy");
      }
    });
  };

  const initializeReleaseManager = (panel) => {
    const listForm = panel.querySelector("[data-release-list-form]");
    const form = panel.querySelector("[data-publish-form]");
    const feedback = panel.querySelector("[data-publish-feedback]");
    const current = panel.querySelector("[data-release-current]");
    let loadedVersion;
    let busy = false;
    const load = async () => {
      if (busy || !listForm.reportValidity()) return;
      busy = true;
      form.hidden = true;
      const version = listForm.elements.minecraft_version.value;
      listForm.querySelector("button").disabled = true;
      setText(current, "正在读取发布列表…");
      try {
        const data = await fetchJsonEnvelope(`/api/releases?minecraft_version=${encodeURIComponent(version)}`);
        if (version !== listForm.elements.minecraft_version.value) return;
        loadedVersion = version;
        form.elements.expected_current_sha256.value = data.current_sha256 || "";
        form.elements.target_release_id.replaceChildren();
        (data.releases || []).forEach((release) => {
          const option = document.createElement("option");
          option.value = release.release_id;
          option.textContent = `${release.release_id} · ${release.built_at || ""}`;
          form.elements.target_release_id.append(option);
        });
        const currentRelease = data.current?.versions?.[version]?.release_id || data.current?.release_id;
        setText(current, currentRelease ? `当前发布：${currentRelease}` : data.current ? "此版本尚未发布。" : "此版本尚未发布。首次发布必须设为默认版本。");
        form.querySelector('[name="set_as_default"][value="false"]').disabled = !data.current;
        form.querySelectorAll('[name="set_as_default"]').forEach((input) => { input.checked = false; });
        form.elements.confirm.checked = false;
        form.querySelector('[value="rollback"]').disabled = !currentRelease;
        form.hidden = !(data.releases || []).length;
        if (data.audit_pending) setText(feedback, "有发布收尾待处理；下一次确认操作会先恢复审计，读取列表不会切换指针。");
        if (form.hidden) setText(current, "此版本暂无候选，请先完成构建。");
      } catch (error) {
        setText(current, `${error.code || "RELEASE_LIST_UNAVAILABLE"} · 读取失败，请重试。`);
      } finally { busy = false; listForm.querySelector("button").disabled = false; }
    };
    listForm.addEventListener("submit", (event) => { event.preventDefault(); setText(feedback, ""); load(); });
    listForm.elements.minecraft_version.addEventListener("input", () => { form.hidden = true; });
    form.elements.target_release_id.addEventListener("change", () => { form.elements.confirm.checked = false; });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (busy || !form.reportValidity() || loadedVersion !== listForm.elements.minecraft_version.value) return;
      const action = event.submitter?.value === "rollback" ? "rollback" : "publish";
      const payload = {
        minecraft_version: loadedVersion,
        target_release_id: form.elements.target_release_id.value,
        expected_current_sha256: form.elements.expected_current_sha256.value || null,
        confirm: form.elements.confirm.checked,
        set_as_default: form.elements.set_as_default.value === "true",
        reviewer: form.elements.reviewer.value.trim(),
        reason: form.elements.reason.value.trim(),
      };
      if (!payload.reviewer || !payload.reason) { setText(feedback, "请填写发布者与原因。"); return; }
      busy = true;
      const controls = Array.from(panel.querySelectorAll("input, select, button"));
      const disabled = controls.map((control) => control.disabled);
      controls.forEach((control) => { control.disabled = true; });
      panel.setAttribute("aria-busy", "true");
      setText(feedback, action === "rollback" ? "正在回滚…" : "正在发布…");
      try {
        const data = await postJsonEnvelope(`/api/releases/${action}`, payload);
        if (!data.applied) throw { code: "RELEASE_NOT_APPLIED" };
        form.hidden = true;
        setText(current, `当前发布：${data.target_release_id}`);
        const warning = data.warnings?.includes("PUBLISH_FINALIZE_PENDING")
          ? "指针已切换，但持久化确认未完成，请在重启后核对当前发布。"
          : data.warnings?.length ? "审计记录待完成；发布已生效，无需重复切换。" : "";
        setText(feedback, `${data.status === "rolled_back" ? "已回滚" : "已发布"}：${data.target_release_id}。${warning}`);
      } catch (error) {
        form.hidden = true;
        setText(feedback, `${error.code || "RELEASE_RESPONSE_UNAVAILABLE"} · ${error.message || "未收到发布结果。"} 请重新读取列表核对当前发布，再决定是否操作。`);
      } finally {
        controls.forEach((control, index) => { control.disabled = disabled[index]; });
        panel.removeAttribute("aria-busy");
        busy = false;
      }
    });
    load();
  };

  body.addEventListener("error", (event) => {
    const image = event.target;
    if (!(image instanceof HTMLImageElement) || !image.matches("[data-ai-plan-image]")) return;
    if (!image.getAttribute("src")) return;
    const confirmation = image.closest("[data-ai-queue-confirmation]");
    const state = aiPlanInspectorStates.get(confirmation);
    const preview = state?.cache.get(state.activeJobId);
    if (preview?.objectUrl) URL.revokeObjectURL(preview.objectUrl);
    if (state?.activeJobId) {
      state.cache.delete(state.activeJobId);
      setAIPlanJobButtonState(state, state.activeJobId, "error", "图片不可读");
      updateAIPlanCacheCount(confirmation, state);
    }
    image.removeAttribute("src");
    image.alt = "";
    const status = confirmation?.querySelector("[data-ai-plan-preview-status]");
    if (status) {
      status.dataset.state = "error";
      status.textContent = "AI_PLAN_IMAGE_INVALID · 本地联系表不可读，请重试此批次预览。";
    }
    announce("批次联系表不可读。 ");
  }, true);

  body.addEventListener("click", (event) => {
    const planJob = event.target.closest("[data-ai-plan-job]");
    if (planJob) {
      event.preventDefault();
      loadAIPlanJobPreview(planJob);
      return;
    }
    const planBack = event.target.closest("[data-ai-plan-preview-back]");
    if (planBack) {
      event.preventDefault();
      focusActiveAIPlanJob(planBack.closest("[data-ai-queue-confirmation]"));
      return;
    }
    const queuePreview = event.target.closest("[data-ai-queue-preview]");
    if (queuePreview) {
      event.preventDefault();
      previewAIQueueCommand(queuePreview);
      return;
    }
    const jobRetry = event.target.closest("[data-ai-job-retry]");
    if (jobRetry) {
      event.preventDefault();
      previewSingleJobRetry(jobRetry);
      return;
    }
    const queueConfirm = event.target.closest("[data-ai-confirm-submit]");
    if (queueConfirm) {
      event.preventDefault();
      submitAIQueueCommand(queueConfirm.closest("[data-ai-queue-confirmation]"));
      return;
    }
    const queueCancel = event.target.closest("[data-ai-confirm-cancel]");
    if (queueCancel) {
      event.preventDefault();
      closeAIQueueConfirmation(queueCancel.closest("[data-ai-queue-confirmation]"));
      return;
    }
    const locate = event.target.closest("[data-locate-current]");
    if (locate) locateCurrentStage(locate.closest("#run-panel"));

  });

  body.addEventListener("keydown", (event) => {
    const planJob = event.target.closest("[data-ai-plan-job]");
    if (planJob && ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      const confirmation = planJob.closest("[data-ai-queue-confirmation]");
      const state = aiPlanInspectorStates.get(confirmation);
      const buttons = state ? Array.from(state.buttons.values()) : [];
      const currentIndex = buttons.indexOf(planJob);
      if (currentIndex >= 0 && buttons.length) {
        event.preventDefault();
        const nextIndex = event.key === "Home"
          ? 0
          : event.key === "End"
            ? buttons.length - 1
            : event.key === "ArrowDown"
              ? Math.min(buttons.length - 1, currentIndex + 1)
              : Math.max(0, currentIndex - 1);
        buttons.forEach((button, index) => { button.tabIndex = index === nextIndex ? 0 : -1; });
        buttons[nextIndex].focus({ preventScroll: true });
        buttons[nextIndex].scrollIntoView({ behavior: reducedMotion.matches ? "auto" : "smooth", block: "nearest" });
      }
      return;
    }
    if (event.key !== "Escape") return;
    const confirmation = document.querySelector("[data-ai-queue-confirmation]:not([hidden])");
    if (!confirmation || confirmation.getAttribute("aria-busy") === "true") return;
    event.preventDefault();
    closeAIQueueConfirmation(confirmation);
  });

  body.addEventListener("submit", (event) => {
    const reviewForm = event.target.closest("[data-review-form]");
    if (!reviewForm) return;
    const status = reviewForm.querySelector("[data-review-form-status]");
    if (!reviewForm.reportValidity() || !serializeReviewOverride(reviewForm)) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (!reviewForm.checkValidity()) {
        status.dataset.state = "error";
        status.textContent = "请先选择动作，并填写审核者、说明与至少一条证据。";
      }
      announce("审核表单尚未完整。 ");
    }
  }, true);

  body.addEventListener("submit", (event) => {
    const importForm = event.target.closest("[data-import-form]");
    if (importForm) {
      event.preventDefault();
      performSelectedAction(importForm);
      return;
    }
    const commandForm = event.target.closest("[data-run-command]");
    if (commandForm) {
      event.preventDefault();
      submitRunCommand(commandForm);
      return;
    }
    const configureAI = event.target.closest("[data-ai-configure]");
    if (configureAI) {
      event.preventDefault();
      submitAIConfigure(configureAI);
      return;
    }
    const approveAI = event.target.closest("[data-ai-approve]");
    if (approveAI) {
      event.preventDefault();
      submitAIBatchAction(approveAI, "approve");
      return;
    }
    const cancelAI = event.target.closest("[data-ai-cancel]");
    if (cancelAI) {
      event.preventDefault();
      submitAIBatchAction(cancelAI, "cancel");
    }
  });

  document.addEventListener("visibilitychange", () => {
    streamManagers.forEach((manager) => {
      if (document.hidden) manager.pauseForVisibility();
      else manager.resumeForVisibility();
    });
  });

  body.addEventListener("htmx:beforeSwap", (event) => {
    const status = event.detail.xhr.status;
    if (status >= 400 && status < 600) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });

  body.addEventListener("htmx:beforeRequest", (event) => {
    const trigger = event.detail.elt;
    const button = trigger.matches("form")
      ? trigger.querySelector('button[type="submit"]')
      : trigger.closest("form")?.querySelector('button[type="submit"]');
    if (button) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    }
  });

  body.addEventListener("htmx:afterRequest", (event) => {
    const trigger = event.detail.elt;
    const button = trigger.matches("form")
      ? trigger.querySelector('button[type="submit"]')
      : trigger.closest("form")?.querySelector('button[type="submit"]');
    if (button) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  });

  body.addEventListener("htmx:afterSwap", (event) => {
    const focusTarget = event.detail.target.querySelector("[data-autofocus]");
    if (focusTarget) {
      focusTarget.focus({ preventScroll: true });
      focusTarget.scrollIntoView({ behavior: reducedMotion.matches ? "auto" : "smooth", block: "nearest" });
    }
    event.detail.target.querySelectorAll("[data-provider-form]").forEach(initializeProviderForm);
    event.detail.target.querySelectorAll("[data-review-form]").forEach(initializeReviewForm);
    event.detail.target.querySelectorAll("[data-explicit-confirmation]").forEach(initializeExplicitConfirmation);
    if (document.querySelector("#provider-profile-list [data-provider-card]")) {
      document.querySelector("#provider-profile-list .provider-empty")?.remove();
    }
    const probeResult = event.detail.target.querySelector("[data-provider-probe-result]");
    if (probeResult) applyProviderProbeView(probeResult);
    updateReviewContinue();
    announce("页面内容已更新。");
  });

  document.querySelectorAll("[data-import-form]").forEach(initializeDirectoryChooser);
  document.querySelectorAll("[data-provider-form]").forEach(initializeProviderForm);
  document.querySelectorAll("[data-review-form]").forEach(initializeReviewForm);
  document.querySelectorAll("[data-explicit-confirmation]").forEach(initializeExplicitConfirmation);
  document.querySelectorAll("[data-ai-control]").forEach(initializeAIControl);
  document.querySelectorAll("[data-release-candidate]").forEach(initializeReleaseCandidate);
  document.querySelectorAll("[data-release-manager]").forEach(initializeReleaseManager);
  updateReviewContinue();
  initializeSnapshotStreams();
  const runPanel = document.getElementById("run-panel");
  if (runPanel) window.requestAnimationFrame(() => locateCurrentStage(runPanel, "auto"));
})();
