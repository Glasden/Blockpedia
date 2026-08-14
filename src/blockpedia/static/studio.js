(() => {
  "use strict";

  const body = document.body;
  const liveRegion = document.getElementById("studio-live-region");
  const streamManagers = new Map();
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
      "passed", "succeeded", "failed", "cancelled", "invalid", "complete", "completed",
    ].includes(status);
  };

  class SnapshotStream {
    constructor(panel) {
      this.panel = panel;
      this.kind = panel.dataset.eventPanel;
      this.url = panel.dataset.eventsUrl;
      this.source = null;
      this.hiddenPause = false;
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
      return this.initialTerminal || ["passed", "succeeded", "failed", "cancelled", "invalid", "complete", "completed"].includes(this.currentStatus);
    }

    open() {
      if (!this.url || this.source || this.settled || document.hidden || !("EventSource" in window)) return;
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
        if (this.source !== source || this.settled || this.hiddenPause) return;
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

    restartAfterCommand() {
      this.settled = false;
      this.hiddenPause = false;
      this.close("等待命令后的最新状态");
      window.setTimeout(() => this.open(), 120);
    }

    handleSnapshot(event) {
      if (document.hidden) return;
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
          this.close(this.currentBoundary ? "已到 R2 边界" : "状态流已结束");
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
        setStreamState(panel, "closed", panel.dataset.initialBoundary ? "已到 R2 边界" : "状态流已结束");
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

  const preflightState = (entry) => {
    const value = String(entry.preflight_status || "").toLowerCase();
    if (["ready", "valid", "passed", "selectable"].includes(value)) return "ready";
    if (["version_mismatch", "mismatch"].includes(value)) return "mismatch";
    if (["invalid", "failed", "error", "staging"].includes(value)) return "invalid";
    if (["checking", "pending", "scanning"].includes(value)) return "checking";
    return entry.selectable ? "ready" : "invalid";
  };

  const checkSubphaseLabels = {
    QUEUED: "等待检查",
    SNAPSHOT_EXPORT: "建立安全快照",
    VALIDATE_EXPORT: "验证导出包",
    SNAPSHOT_INVENTORY: "枚举快照文件",
    SNAPSHOT_COPY_HASH: "复制并计算哈希",
    SNAPSHOT_METADATA: "写入快照元数据",
    INVENTORY: "清点导出内容",
    SCHEMAS_MANIFEST: "加载 Schema 与 manifest",
    JSONL_RECORDS: "读取业务记录",
    CROSS_REFERENCES: "核对跨记录引用",
    RENDERS: "检查预览与蒙版",
    CHECKSUMS: "复算 checksums",
    FINALIZE: "汇总检查结果",
  };

  const workflowForEntry = (entry) => {
    const marker = entry?.check_marker && typeof entry.check_marker === "object" ? entry.check_marker : {};
    const markerState = String(marker.state || "unchecked");
    const workspace = marker.workspace && typeof marker.workspace === "object" ? marker.workspace : {};
    const runId = marker.run_id || workspace.run_id || null;
    const runUrl = marker.run_url || workspace.run_url || null;
    const workspaceStatus = String(workspace.status || (runId && !runUrl ? "creating" : "absent"));
    let state = markerState;
    if (["pending", "running"].includes(marker.status) || markerState === "checking") state = "checking";
    else if (markerState === "changed_since_check") state = "changed_since_check";
    else if (markerState === "failed") state = "failed";
    else if (["pending", "running", "creating"].includes(workspaceStatus)) state = "creating";
    else if (runId && runUrl) state = "imported";
    else if (markerState === "checked") state = "checked";
    else state = "unchecked";
    return {
      state,
      marker,
      workspace,
      checkId: marker.check_id || null,
      checkUrl: marker.check_url || (marker.check_id ? `/imports/checks/${encodeURIComponent(marker.check_id)}` : null),
      runId,
      runUrl,
      progress: marker.progress && typeof marker.progress === "object" ? marker.progress : {},
    };
  };

  const workflowCopy = (workflow, entry) => {
    const exportId = entry.export_id || entry.label || entry.name || "当前导出";
    const subphase = checkSubphaseLabels[workflow.marker.subphase] || workflow.marker.subphase || workflow.marker.phase || "等待检查";
    if (workflow.state === "checking") return { badge: "检查中", badgeClass: "status-badge--running", mark: "●", markClass: "checking", title: exportId, detail: `${subphase}；已有检查正在执行。`, action: "view", actionLabel: "查看进度" };
    if (workflow.state === "creating") return { badge: "正在创建运行", badgeClass: "status-badge--running", mark: "✓", markClass: "checked", title: exportId, detail: "已检查快照正在创建工作区；不会重复导入。", action: "view", actionLabel: "正在创建运行" };
    if (workflow.state === "imported") return { badge: "已有运行", badgeClass: "status-badge--running", mark: "✓", markClass: "imported", title: exportId, detail: `已检查并关联运行 ${workflow.runId || ""}。`, action: "run", actionLabel: "进入现有运行" };
    if (workflow.state === "checked") return { badge: "✓ 已检查", badgeClass: "status-badge--succeeded", mark: "✓", markClass: "checked", title: exportId, detail: "安全快照已通过完整性检查，等待创建运行。", action: "import", actionLabel: "导入并进入运行" };
    if (workflow.state === "changed_since_check") return { badge: "来源已变化", badgeClass: "status-badge--paused", mark: "!", markClass: "changed", title: exportId, detail: "旧检查快照仍保留；当前来源必须使用新引用重新检查。", action: "start", actionLabel: "重新检查" };
    if (workflow.state === "failed") return { badge: workflow.marker.error_code === "IMPORT_CHECK_INTERRUPTED" ? "检查中断" : "检查失败", badgeClass: "status-badge--failed", mark: "!", markClass: "failed", title: exportId, detail: `${workflow.marker.error_code || "IMPORT_CHECK_FAILED"}；可使用当前新引用重新检查。`, action: "start", actionLabel: "重新检查" };
    return { badge: "未检查", badgeClass: "", mark: "○", markClass: "unchecked", title: exportId, detail: "入口文件预检通过；尚未建立安全检查快照。", action: "start", actionLabel: "开始检查" };
  };

  const initializeDirectoryChooser = (form) => {
    const version = form.querySelector("[data-directory-version]");
    const browse = form.querySelector("[data-directory-browse]");
    const chooser = form.querySelector("[data-directory-chooser]");
    const close = form.querySelector("[data-directory-close]");
    const parent = form.querySelector("[data-directory-parent]");
    const entries = form.querySelector("[data-directory-entries]");
    const location = form.querySelector("[data-directory-location]");
    const browserStatus = form.querySelector("[data-directory-browser-status]");
    const reference = form.querySelector("[data-directory-ref]");
    const display = form.querySelector("[data-directory-display]");
    const submit = form.querySelector("[data-import-check-submit]");
    const manual = form.querySelector("[data-manual-directory-ref]");
    const useManual = form.querySelector("[data-use-manual-ref]");
    const selectedWorkflow = form.querySelector("[data-selected-workflow]");
    const selectedMarker = form.querySelector("[data-selected-marker]");
    const selectedBadge = form.querySelector("[data-selected-badge]");
    const selectedTitle = form.querySelector("[data-selected-title]");
    const selectedDetail = form.querySelector("[data-selected-detail]");
    const selectedProgress = form.querySelector("[data-selected-progress]");
    const selectedProgressBar = form.querySelector("[data-selected-progress-bar]");
    const selectedProgressCopy = form.querySelector("[data-selected-progress-copy]");
    const actionLabel = form.querySelector("[data-selected-action-label]");
    let parentReference = null;
    let pendingFocusExportId = null;

    const clearSelection = () => {
      reference.value = "";
      display.value = "";
      submit.disabled = true;
      submit.dataset.selectedAction = "start";
      delete form.dataset.selectedCheckId;
      delete form.dataset.selectedCheckUrl;
      delete form.dataset.selectedRunUrl;
      selectedWorkflow.hidden = true;
      directoryFeedback(form, "neutral", "尚未选择导出目录。");
      setText(form.querySelector("[data-import-start-status]"), "选择可检查的导出后继续。");
    };

    const renderSelectedWorkflow = (entry) => {
      const workflow = workflowForEntry(entry);
      const copy = workflowCopy(workflow, entry);
      const ready = preflightState(entry) === "ready";
      selectedWorkflow.hidden = false;
      selectedWorkflow.dataset.state = workflow.state;
      selectedMarker.textContent = copy.mark;
      selectedMarker.className = `export-state-mark export-state-mark--${copy.markClass}`;
      selectedBadge.textContent = copy.badge;
      selectedBadge.className = `status-badge ${copy.badgeClass}`;
      selectedTitle.textContent = copy.title;
      selectedDetail.textContent = copy.detail;
      submit.dataset.selectedAction = copy.action;
      setText(actionLabel, copy.actionLabel);
      form.dataset.selectedCheckId = workflow.checkId || "";
      form.dataset.selectedCheckUrl = workflow.checkUrl || "";
      form.dataset.selectedRunUrl = workflow.runUrl || "";
      submit.disabled = (copy.action === "start" && (!ready || !entry.directory_ref)) || (copy.action === "view" && !workflow.checkUrl) || (copy.action === "import" && !workflow.checkId) || (copy.action === "run" && !workflow.runUrl);
      const progress = workflow.progress;
      if (workflow.state === "checking") {
        const completed = Number(progress.completed || 0);
        const total = Number(progress.total || 0);
        selectedProgress.hidden = false;
        if (total > 0) {
          selectedProgressBar.value = Math.min(completed, total);
          selectedProgressBar.max = total;
          selectedProgressBar.classList.remove("indeterminate-progress");
        } else {
          selectedProgressBar.removeAttribute("value");
          selectedProgressBar.removeAttribute("max");
          selectedProgressBar.classList.add("indeterminate-progress");
        }
        const subphase = checkSubphaseLabels[workflow.marker.subphase] || workflow.marker.subphase || "检查中";
        selectedProgressBar.setAttribute("aria-label", total > 0 ? `${subphase}，已完成 ${completed}，共 ${total}` : `${subphase}正在执行，已处理 ${completed}`);
        setText(selectedProgressCopy, `${subphase} · ${total > 0 ? `${completed}/${total}` : `已处理 ${completed}`} ${progress.unit || "items"}`);
      } else {
        selectedProgress.hidden = true;
      }
      return { workflow, copy };
    };

    const selectEntry = (entry) => {
      const state = preflightState(entry);
      const label = entry.export_id || entry.label || entry.name || "已选择本地导出";
      const selectedVersion = entry.minecraft_version || version.value;
      reference.value = entry.directory_ref || "";
      display.value = `${selectedVersion} / ${label}`;
      const selected = renderSelectedWorkflow(entry);
      if (state === "ready") {
        const feedbackCopy = selected.workflow.state === "checking" ? "该导出已有检查正在执行；不会创建重复检查。" : selected.workflow.state === "checked" ? "该导出已有通过的检查快照。" : selected.workflow.state === "imported" ? "该导出已关联现有运行。" : "目录结构可识别；仍需运行完整性检查。";
        directoryFeedback(form, "ready", feedbackCopy);
        setText(form.querySelector("[data-import-start-status]"), selected.copy.action === "start" ? "已准备好开始完整性检查。" : "使用当前状态操作继续。");
      } else if (state === "mismatch") {
        directoryFeedback(form, "mismatch", "导出版本与当前显式版本不一致。", entry.error_code || "RELEASE_VERSION_MISMATCH");
      } else if (state === "checking") {
        directoryFeedback(form, "checking", "目录仍在预检，请稍后重新选择。");
      } else {
        directoryFeedback(form, "invalid", "该目录当前不能进入完整性检查。", entry.error_code || "IMPORT_INCOMPLETE");
      }
      chooser.hidden = true;
      browse.setAttribute("aria-expanded", "false");
      browse.focus({ preventScroll: true });
    };

    const makeActionButton = (label, onClick, quiet = true) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `button ${quiet ? "button--quiet" : "button--primary"}`;
      button.textContent = label;
      button.addEventListener("click", onClick);
      return button;
    };

    const renderEntries = (directory) => {
      entries.replaceChildren();
      const list = Array.isArray(directory.entries) ? directory.entries : [];
      if (!list.length) {
        const empty = document.createElement("p");
        empty.className = "directory-entry-empty";
        empty.textContent = "所选位置没有可用导出。";
        entries.append(empty);
        return;
      }
      list.forEach((entry) => {
        const row = document.createElement("article");
        row.className = "directory-entry";
        row.dataset.exportId = entry.export_id || "";
        const identity = document.createElement("div");
        identity.className = "directory-entry__identity";
        const workflow = workflowForEntry(entry);
        const workflowText = workflowCopy(workflow, entry);
        const badges = document.createElement("div");
        badges.className = "directory-entry__badges";
        const badge = document.createElement("span");
        badge.className = `status-badge ${workflowText.badgeClass}`;
        badge.textContent = workflowText.badge;
        badges.append(badge);
        if (["checked", "creating", "imported", "changed_since_check"].includes(workflow.state)) {
          const checked = document.createElement("span");
          checked.className = "checked-chip";
          checked.textContent = "✓ 已检查快照";
          badges.append(checked);
        }
        identity.append(badges);
        const name = document.createElement("b");
        name.textContent = entry.label || entry.name || entry.export_id || "未命名目录";
        identity.append(name);
        if (entry.export_id) {
          const code = document.createElement("code");
          code.textContent = entry.export_id;
          identity.append(code);
        }
        const detail = document.createElement("small");
        const state = preflightState(entry);
        detail.textContent = entry.error_code
          ? `${entry.error_code} · ${entry.preflight_status || "不可选择"}`
          : `${entry.minecraft_version || version.value} · ${workflow.state === "checking" ? (checkSubphaseLabels[workflow.marker.subphase] || workflow.marker.subphase || "检查中") : (entry.preflight_status || (entry.selectable ? "ready" : "目录"))}`;
        identity.append(detail);

        if (workflow.state === "checking") {
          const compact = document.createElement("div");
          compact.className = "compact-check-progress";
          const bar = document.createElement("progress");
          const completed = Number(workflow.progress.completed || 0);
          const total = Number(workflow.progress.total || 0);
          if (total > 0) {
            bar.value = Math.min(completed, total);
            bar.max = total;
          } else {
            bar.className = "indeterminate-progress";
          }
          const subphase = checkSubphaseLabels[workflow.marker.subphase] || workflow.marker.subphase || "检查中";
          bar.setAttribute("aria-label", total > 0 ? `${subphase}，已完成 ${completed}，共 ${total}` : `${subphase}正在执行，已处理 ${completed}`);
          const progressCopy = document.createElement("span");
          progressCopy.textContent = total > 0 ? `${completed}/${total} ${workflow.progress.unit || "items"}` : `已处理 ${completed} ${workflow.progress.unit || "items"}`;
          compact.append(bar, progressCopy);
          identity.append(compact);
        }

        const actions = document.createElement("div");
        actions.className = "directory-entry__actions";
        if (entry.can_enter && entry.directory_ref && !entry.selectable) {
          actions.append(makeActionButton("打开", () => loadDirectory(entry.directory_ref)));
        }
        if (entry.directory_ref) {
          actions.append(makeActionButton("选择", () => selectEntry(entry)));
        }
        const actionUsesFreshRef = workflowText.action === "start";
        if ((entry.selectable && entry.directory_ref) || (!actionUsesFreshRef && ["view", "run", "import"].includes(workflowText.action))) {
          const primary = makeActionButton(workflowText.actionLabel, () => {
            selectEntry(entry);
            window.setTimeout(() => performSelectedAction(form, primary), 0);
          }, workflowText.action !== "start" && workflowText.action !== "import");
          if ((workflowText.action === "view" && !workflow.checkUrl) || (workflowText.action === "run" && !workflow.runUrl) || (workflowText.action === "import" && !workflow.checkId)) primary.disabled = true;
          actions.append(primary);
        } else if (entry.error_code) {
          actions.append(makeActionButton("查看状态", () => selectEntry(entry)));
        }
        row.append(identity, actions);
        entries.append(row);
      });
    };

    const loadDirectory = async (parentRef = "") => {
      if (!version.checkValidity()) {
        version.reportValidity();
        return;
      }
      chooser.hidden = false;
      browse.setAttribute("aria-expanded", "true");
      directoryFeedback(form, "checking", "正在读取已配置 data-root 中的导出目录…");
      setText(browserStatus, "正在读取目录…");
      entries.replaceChildren();
      const url = new URL("/api/directories", window.location.origin);
      url.searchParams.set("minecraft_version", version.value);
      if (parentRef) url.searchParams.set("parent_ref", parentRef);
      try {
        const response = await fetch(url, { headers: { Accept: "application/json" } });
        const envelope = await response.json();
        if (!response.ok || envelope.ok === false) {
          throw new Error(envelope.error_code || "DIRECTORY_BROWSER_UNAVAILABLE");
        }
        const directory = envelope.data || {};
        parentReference = directory.parent_ref || "";
        parent.hidden = !parentReference;
        setText(location, directory.label || `Minecraft ${version.value} 导出`);
        setText(browserStatus, `${Array.isArray(directory.entries) ? directory.entries.length : 0} 个目录项`);
        renderEntries(directory);
        directoryFeedback(form, "neutral", "请选择一个可检查的 exporter 导出。 ");
        const focusRow = pendingFocusExportId
          ? Array.from(entries.querySelectorAll("[data-export-id]")).find((row) => row.dataset.exportId === pendingFocusExportId)
          : null;
        pendingFocusExportId = null;
        (focusRow?.querySelector("button") || entries.querySelector("button"))?.focus({ preventScroll: true });
      } catch (error) {
        const code = error instanceof Error ? error.message : "DIRECTORY_BROWSER_UNAVAILABLE";
        setText(browserStatus, "目录浏览暂不可用。");
        directoryFeedback(form, "invalid", "无法读取本地导出列表，请稍后重试。", code);
      }
    };

    browse.addEventListener("click", () => {
      if (!chooser.hidden) {
        chooser.hidden = true;
        browse.setAttribute("aria-expanded", "false");
        return;
      }
      loadDirectory();
    });
    close.addEventListener("click", () => {
      chooser.hidden = true;
      browse.setAttribute("aria-expanded", "false");
      browse.focus({ preventScroll: true });
    });
    parent.addEventListener("click", () => loadDirectory(parentReference));
    version.addEventListener("change", clearSelection);
    useManual.addEventListener("click", () => {
      const value = manual.value.trim();
      const looksAbsolute = /^[a-zA-Z]:[\\/]/.test(value) || value.startsWith("/") || value.startsWith("\\\\");
      if (!value || looksAbsolute) {
        manual.setAttribute("aria-invalid", "true");
        directoryFeedback(form, "invalid", "请输入 Studio 提供的不透明引用；绝对路径不会被接受。", "INVALID_INPUT");
        submit.disabled = true;
        return;
      }
      manual.setAttribute("aria-invalid", "false");
      const entry = { directory_ref: value, export_id: "手动目录引用", minecraft_version: version.value, selectable: true, preflight_status: "ready", check_marker: { state: "unchecked" } };
      reference.value = value;
      display.value = "已选择手动目录引用";
      renderSelectedWorkflow(entry);
      directoryFeedback(form, "ready", "已接收不透明引用；服务将在完整检查前验证它。 ");
      setText(form.querySelector("[data-import-start-status]"), "已准备好开始完整性检查。");
    });

    const openForExport = (exportId, minecraftVersion) => {
      if (minecraftVersion) version.value = minecraftVersion;
      clearSelection();
      pendingFocusExportId = exportId || null;
      chooser.hidden = false;
      browse.setAttribute("aria-expanded", "true");
      chooser.scrollIntoView({ behavior: reducedMotion.matches ? "auto" : "smooth", block: "nearest" });
      loadDirectory();
    };

    form.addEventListener("directory:reselect", (event) => {
      openForExport(event.detail?.exportId, event.detail?.minecraftVersion);
    });
  };

  const showImportStartError = (form, code, message) => {
    const target = form.closest(".work-card")?.querySelector("[data-import-start-feedback]");
    if (!target) return;
    const panel = document.createElement("section");
    panel.className = "inline-error";
    panel.setAttribute("role", "alert");
    const errorCode = document.createElement("span");
    errorCode.className = "error-code";
    errorCode.textContent = code || "IMPORT_CHECK_START_FAILED";
    const heading = document.createElement("h3");
    heading.textContent = message || "完整性检查未启动。";
    const repair = document.createElement("p");
    repair.textContent = "请确认版本和目录引用后重试。";
    panel.append(errorCode, heading, repair);
    target.replaceChildren(panel);
  };

  const showImportStarting = (form) => {
    const target = form.closest(".work-card")?.querySelector("[data-import-start-feedback]");
    if (!target) return;
    const panel = document.createElement("section");
    panel.className = "check-starting";
    panel.setAttribute("role", "status");
    const mark = document.createElement("span");
    mark.className = "check-starting__mark";
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = "↻";
    const copy = document.createElement("div");
    const heading = document.createElement("h3");
    heading.textContent = "正在查找完整性检查";
    const text = document.createElement("p");
    text.textContent = "若该导出已有活动或通过的检查，将直接进入权威进度页，不会重复启动。";
    copy.append(heading, text);
    panel.append(mark, copy);
    target.replaceChildren(panel);
  };

  const startImportCheck = async (form, triggerButton = null) => {
    const reference = form.querySelector("[data-directory-ref]")?.value.trim();
    const version = form.querySelector("[data-directory-version]")?.value.trim();
    const submit = form.querySelector("[data-import-check-submit]");
    const trigger = triggerButton || submit;
    if (!reference || !version || !form.reportValidity()) return;
    submit.disabled = true;
    trigger.disabled = true;
    submit.setAttribute("aria-busy", "true");
    trigger.setAttribute("aria-busy", "true");
    setText(form.querySelector("[data-import-start-status]"), "正在查找已有检查或准备安全快照…");
    showImportStarting(form);
    try {
      const response = await fetch(form.action, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json, text/html" },
        body: JSON.stringify({ source_directory: reference, minecraft_version: version }),
      });
      if (response.redirected) {
        const redirected = safeSameOriginLocation(response.url);
        if (redirected?.pathname.startsWith("/imports/checks/")) {
          window.location.assign(redirected.href);
          return;
        }
      }
      const contentType = response.headers.get("content-type") || "";
      const envelope = contentType.includes("application/json") ? await response.json() : null;
      if (!response.ok || envelope?.ok === false) {
        throw { code: envelope?.error_code || "IMPORT_CHECK_START_FAILED", message: envelope?.message || "完整性检查未启动。" };
      }
      const data = envelope?.data || {};
      const checkId = data.check_id || data.snapshot?.check_id;
      const location = safeSameOriginLocation(data.canonical_url || data.location || data.url);
      if (location?.pathname.startsWith("/imports/checks/")) {
        window.location.assign(location.href);
        return;
      }
      if (checkId) {
        window.location.assign(`/imports/checks/${encodeURIComponent(checkId)}`);
        return;
      }
      throw { code: "IMPORT_CHECK_START_FAILED", message: "服务没有返回检查标识。" };
    } catch (error) {
      const code = error?.code || "IMPORT_CHECK_START_FAILED";
      const message = error?.message || "完整性检查未启动。";
      showImportStartError(form, code, message);
      submit.disabled = false;
      trigger.disabled = false;
      submit.removeAttribute("aria-busy");
      trigger.removeAttribute("aria-busy");
      setText(form.querySelector("[data-import-start-status]"), "检查未启动，可以修正后重试。");
      announce("完整性检查未启动，请查看错误信息。");
    }
  };

  const showActionError = (owner, code, message) => {
    owner.querySelector("[data-action-error]")?.remove();
    const error = document.createElement("p");
    error.className = "action-error";
    error.dataset.actionError = "true";
    error.setAttribute("role", "alert");
    const stableCode = document.createElement("code");
    stableCode.textContent = code || "IMPORT_FAILED";
    error.append(stableCode, document.createTextNode(` · ${message || "运行未创建，请按错误码处理。"}`));
    owner.append(error);
  };

  const submitImportRequest = async ({ checkId, checkUrl, button, owner }) => {
    if (!checkId || !button) return;
    const originalLabel = button.textContent;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "正在创建运行…";
    owner.querySelector("[data-action-error]")?.remove();
    try {
      const response = await fetch("/api/imports", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ check_id: checkId, copy_mode: "copy_to_workspace" }),
      });
      const envelope = await response.json().catch(() => null);
      if (!response.ok || envelope?.ok === false) {
        throw { code: envelope?.error_code || "IMPORT_FAILED", message: envelope?.message || "运行未创建。" };
      }
      const data = envelope?.data || {};
      const workspace = data.workspace && typeof data.workspace === "object" ? data.workspace : {};
      const runId = data.run_id || workspace.run_id;
      const runUrl = safeSameOriginLocation(data.run_url || workspace.run_url || (runId ? `/runs/${encodeURIComponent(runId)}` : null));
      if ([200, 201].includes(response.status) && runId && runUrl) {
        window.location.assign(runUrl.href);
        return;
      }
      if (response.status === 202) {
        const canonical = safeSameOriginLocation(data.canonical_url || checkUrl || `/imports/checks/${encodeURIComponent(checkId)}`);
        const canonicalPanel = document.getElementById("import-check-panel");
        if (canonicalPanel) {
          const feedback = document.getElementById("import-action-feedback");
          setText(feedback, "运行已保留，正在创建工作区；等待权威状态快照。");
          streamManagers.get("import-check-panel")?.restartAfterCommand();
          button.textContent = "正在创建运行";
        } else if (canonical) {
          window.location.assign(canonical.href);
        }
        return;
      }
      throw { code: "IMPORT_RESULT_INVALID", message: "服务没有返回可进入的运行。" };
    } catch (error) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = originalLabel;
      showActionError(owner, error?.code || "IMPORT_FAILED", error?.message || "运行未创建。 ");
      announce("运行未创建，请查看稳定错误码。");
    }
  };

  const submitImportAction = (form) => {
    const button = form.querySelector('button[type="submit"]');
    const owner = form.closest(".recent-check-row, .import-progress") || form.parentElement;
    return submitImportRequest({
      checkId: form.querySelector('[name="check_id"]')?.value,
      checkUrl: form.dataset.checkUrl,
      button,
      owner,
    });
  };

  const performSelectedAction = (form, triggerButton = null) => {
    const submit = form.querySelector("[data-selected-primary]");
    const button = triggerButton || submit;
    const action = submit.dataset.selectedAction || "start";
    if (action === "view") {
      const target = safeSameOriginLocation(form.dataset.selectedCheckUrl);
      if (target) window.location.assign(target.href);
      return;
    }
    if (action === "run") {
      const target = safeSameOriginLocation(form.dataset.selectedRunUrl);
      if (target) window.location.assign(target.href);
      return;
    }
    if (action === "import") {
      submitImportRequest({
        checkId: form.dataset.selectedCheckId,
        checkUrl: form.dataset.selectedCheckUrl,
        button,
        owner: form.closest(".work-card") || form,
      });
      return;
    }
    startImportCheck(form, button);
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

  body.addEventListener("click", (event) => {
    const locate = event.target.closest("[data-locate-current]");
    if (locate) locateCurrentStage(locate.closest("#run-panel"));
    const reselect = event.target.closest("[data-reselect-export]");
    if (reselect) {
      const chooserForm = document.querySelector("[data-import-check-form]");
      chooserForm?.dispatchEvent(new CustomEvent("directory:reselect", {
        detail: {
          exportId: reselect.dataset.exportId,
          minecraftVersion: reselect.dataset.minecraftVersion,
        },
      }));
    }
  });

  body.addEventListener("submit", (event) => {
    const importForm = event.target.closest("[data-import-check-form]");
    if (importForm) {
      event.preventDefault();
      performSelectedAction(importForm);
      return;
    }
    const importAction = event.target.closest("[data-import-action]");
    if (importAction) {
      event.preventDefault();
      submitImportAction(importAction);
      return;
    }
    const commandForm = event.target.closest("[data-run-command]");
    if (commandForm) {
      event.preventDefault();
      submitRunCommand(commandForm);
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
    announce("页面内容已更新。");
  });

  document.querySelectorAll("[data-import-check-form]").forEach(initializeDirectoryChooser);
  initializeSnapshotStreams();
  const runPanel = document.getElementById("run-panel");
  if (runPanel) window.requestAnimationFrame(() => locateCurrentStage(runPanel, "auto"));
})();
