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
      return this.initialTerminal || ["passed", "succeeded", "failed", "cancelled", "invalid", "complete", "completed"].includes(this.currentStatus);
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
          && this.currentBoundary === "R3_BOUNDARY_REACHED_BUILD_RELEASE_PENDING"
          && !document.querySelector("[data-release-candidate]")
        ) {
          announce("运行已到候选构建边界，正在打开候选检查面板。 ");
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
        const runId = safeAIQueueId(panel.dataset.runId);
        const planJobs = runId ? normalizeAIPlanJobs(data.jobs, count, runId) : null;
        if (!adapter || !model || !planJobs) throw { code: "AI_QUEUE_PREVIEW_INVALID" };
        openAIQueueConfirmation(panel, button, {
          kind,
          hash,
          count,
          adapter,
          model,
          planJobs,
          title: "确认自动处理剩余批次",
          hashLabel: "不可变计划哈希",
          copy: "确认后会向所选 provider 提交这些批次。手动逐批批准仍是默认方式；自动模式按不可变计划顺序一次处理一个批次。普通失败会记录后继续，认证或配置等致命错误会停止。",
          submitLabel: "确认并开始顺序处理",
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

  const candidateSafeId = (value) => {
    const text = String(value || "");
    return /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/.test(text) ? text : "不可显示";
  };

  const candidateSafeHash = (value) => {
    const text = String(value || "");
    return /^sha256:[0-9a-f]{64}$/.test(text) ? text : "未报告";
  };

  const candidateSafeTime = (value) => {
    const text = String(value || "");
    return /^\d{4}-\d{2}-\d{2}T[0-9:.+-]+Z?$/.test(text) && text.length <= 64 ? text : "未报告";
  };

  const candidateSafeMessage = (value, fallback) => {
    const text = String(value || "");
    if (!text || text.length > 300 || text.includes("/") || text.includes("\\")) return fallback;
    return text;
  };

  const setCandidateField = (panel, name, value) => {
    const field = panel.querySelector(`[data-candidate-field="${name}"]`);
    setText(field, value);
    if (field?.tagName === "TIME") {
      if (value === "未报告") field.removeAttribute("datetime");
      else field.setAttribute("datetime", value);
    }
  };

  const setCandidateBusy = (button, busy, pendingLabel) => {
    if (!button.dataset.idleLabel) button.dataset.idleLabel = button.textContent.trim();
    button.disabled = busy;
    button.toggleAttribute("aria-busy", busy);
    button.textContent = busy ? pendingLabel : button.dataset.idleLabel;
  };

  const showCandidateError = (panel, error) => {
    const box = panel.querySelector("[data-candidate-error]");
    const code = /^[A-Z][A-Z0-9_]{1,127}$/.test(String(error?.code || "")) ? String(error.code) : "RELEASE_CHECK_FAILED";
    box.hidden = false;
    setText(box.querySelector("[data-candidate-error-code]"), code);
    setText(box.querySelector("[data-candidate-error-message]"), candidateSafeMessage(error?.message, "候选操作未完成，请按错误码处理。"));
    panel.dataset.state = "error";
    const badge = panel.querySelector("[data-candidate-badge]");
    badge.className = "status-badge status-badge--failed";
    badge.textContent = "操作未完成";
    setText(panel.querySelector("[data-candidate-message]"), `${code} · 请处理后重试。`);
    announce(`候选操作未完成：${code}。`);
  };

  const renderCandidateCheck = (panel, data) => {
    const runId = panel.dataset.candidateRunId;
    const version = panel.dataset.candidateVersion;
    if (data.run_id !== runId || data.minecraft_version !== version) {
      throw { code: "RELEASE_VERSION_MISMATCH", message: "候选检查结果与当前运行不一致。" };
    }
    const checkId = candidateSafeId(data.check_id);
    if (checkId === "不可显示") throw { code: "RELEASE_CHECK_RESULT_INVALID", message: "候选检查标识不合法。" };
    panel.dataset.candidateCheckId = checkId;
    panel.querySelector("[data-candidate-error]").hidden = true;
    panel.querySelector("[data-candidate-check-result]").hidden = false;
    setCandidateField(panel, "check_id", checkId);
    setCandidateField(panel, "release_build_id", candidateSafeId(data.release_build_id));
    setCandidateField(panel, "snapshot_fingerprint", candidateSafeHash(data.snapshot_fingerprint));
    setCandidateField(panel, "quality_report_sha256", candidateSafeHash(data.quality_report_sha256));
    setCandidateField(panel, "created_at", candidateSafeTime(data.created_at));
    setCandidateField(panel, "updated_at", candidateSafeTime(data.updated_at));
    const canBuild = data.can_build === true;
    panel.dataset.state = canBuild ? "buildable" : "blocked";
    const badge = panel.querySelector("[data-candidate-badge]");
    const resultBadge = panel.querySelector("[data-candidate-result-badge]");
    badge.className = `status-badge status-badge--${canBuild ? "succeeded" : "failed"}`;
    resultBadge.className = `status-badge status-badge--${canBuild ? "succeeded" : "failed"}`;
    badge.textContent = canBuild ? "可以构建" : "存在阻断";
    resultBadge.textContent = canBuild ? "buildable" : "blocked";
    setText(panel.querySelector("[data-candidate-message]"), canBuild ? "当前检查快照允许构建不可变候选。" : "当前检查快照存在阻断项。 ");
    panel.querySelector("[data-candidate-blocked]").hidden = canBuild;
    const buildAction = panel.querySelector("[data-candidate-build-action]");
    const buildButton = panel.querySelector("[data-candidate-build]");
    buildAction.hidden = !canBuild;
    buildButton.disabled = !canBuild;
    panel.querySelector('[data-candidate-step="check"]').className = canBuild ? "is-complete" : "is-blocked";
    panel.querySelector('[data-candidate-step="build"]').className = canBuild ? "is-current" : "";
    announce(canBuild ? "候选检查通过，可以构建。" : "候选检查存在阻断项。 ");
  };

  const renderCandidateHashes = (panel, data) => {
    const orderedHashes = [
      ["manifest_sha256", data.manifest_sha256],
      ["quality_report_sha256", data.quality_report_sha256],
      ["checksums_sha256", data.checksums_sha256],
    ].map(([name, value]) => {
      const hash = candidateSafeHash(value);
      if (hash === "未报告") {
        throw { code: "RELEASE_BUILD_RESULT_INVALID", message: "候选构建摘要不完整。" };
      }
      return [name, hash];
    });
    const owner = panel.querySelector("[data-candidate-hashes]");
    owner.replaceChildren();
    orderedHashes.forEach(([name, hash]) => {
      const row = document.createElement("span");
      const label = document.createElement("b");
      const code = document.createElement("code");
      label.textContent = name;
      code.textContent = hash;
      row.append(label, code);
      owner.append(row);
    });
  };

  const renderCandidateBuilt = (panel, data) => {
    if (data.status !== "built") throw { code: "RELEASE_BUILD_RESULT_INVALID", message: "候选构建结果状态不合法。" };
    const releaseId = candidateSafeId(data.release_id);
    if (releaseId === "不可显示") throw { code: "RELEASE_BUILD_RESULT_INVALID", message: "候选标识不合法。" };
    renderCandidateHashes(panel, data);
    panel.dataset.state = "built";
    panel.querySelector("[data-candidate-error]").hidden = true;
    panel.querySelector("[data-candidate-build-action]").hidden = true;
    panel.querySelector("[data-candidate-built]").hidden = false;
    setText(panel.querySelector('[data-candidate-built-field="release_id"]'), releaseId);
    const builtAt = candidateSafeTime(data.built_at);
    const builtAtField = panel.querySelector('[data-candidate-built-field="built_at"]');
    setText(builtAtField, builtAt);
    if (builtAt === "未报告") builtAtField.removeAttribute("datetime");
    else builtAtField.setAttribute("datetime", builtAt);
    const badge = panel.querySelector("[data-candidate-badge]");
    badge.className = "status-badge status-badge--succeeded";
    badge.textContent = "候选已构建";
    setText(panel.querySelector("[data-candidate-message]"), "不可变候选已构建，保持未激活。 ");
    panel.querySelector('[data-candidate-step="build"]').className = "is-complete";
    panel.querySelector("[data-candidate-check]").disabled = true;
    announce("不可变候选已构建，仍未激活。 ");
  };

  const runCandidateCheck = async (panel) => {
    const button = panel.querySelector("[data-candidate-check]");
    setCandidateBusy(button, true, "正在检查…");
    panel.querySelector("[data-candidate-error]").hidden = true;
    panel.dataset.state = "checking";
    const badge = panel.querySelector("[data-candidate-badge]");
    badge.className = "status-badge status-badge--running";
    badge.textContent = "检查中";
    setText(panel.querySelector("[data-candidate-message]"), "正在检查当前运行快照。 ");
    try {
      const data = await postJsonEnvelope(panel.dataset.candidateCheckRoute, {
        run_id: panel.dataset.candidateRunId,
        minecraft_version: panel.dataset.candidateVersion,
      });
      renderCandidateCheck(panel, data);
    } catch (error) {
      showCandidateError(panel, error);
    } finally {
      setCandidateBusy(button, false, "正在检查…");
    }
  };

  const buildCandidate = async (panel) => {
    const button = panel.querySelector("[data-candidate-build]");
    const checkId = panel.dataset.candidateCheckId;
    if (!checkId) return;
    setCandidateBusy(button, true, "正在构建…");
    panel.querySelector("[data-candidate-error]").hidden = true;
    panel.dataset.state = "building";
    const badge = panel.querySelector("[data-candidate-badge]");
    badge.className = "status-badge status-badge--running";
    badge.textContent = "构建中";
    setText(panel.querySelector("[data-candidate-message]"), "正在构建不可变候选。 ");
    try {
      const data = await postJsonEnvelope(panel.dataset.candidateBuildRoute, {
        check_id: checkId,
        confirm_immutable_release: true,
      });
      renderCandidateBuilt(panel, data);
    } catch (error) {
      showCandidateError(panel, error);
      button.disabled = false;
    } finally {
      button.removeAttribute("aria-busy");
      if (panel.dataset.state !== "built") button.textContent = button.dataset.idleLabel;
    }
  };

  const initializeReleaseCandidate = (panel) => {
    if (panel.dataset.candidateReady === "true") return;
    panel.dataset.candidateReady = "true";
    panel.querySelector("[data-candidate-check]")?.addEventListener("click", () => runCandidateCheck(panel));
    panel.querySelector("[data-candidate-build]")?.addEventListener("click", () => buildCandidate(panel));
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
    if (document.querySelector("#provider-profile-list [data-provider-card]")) {
      document.querySelector("#provider-profile-list .provider-empty")?.remove();
    }
    const probeResult = event.detail.target.querySelector("[data-provider-probe-result]");
    if (probeResult) applyProviderProbeView(probeResult);
    updateReviewContinue();
    announce("页面内容已更新。");
  });

  document.querySelectorAll("[data-import-check-form]").forEach(initializeDirectoryChooser);
  document.querySelectorAll("[data-provider-form]").forEach(initializeProviderForm);
  document.querySelectorAll("[data-review-form]").forEach(initializeReviewForm);
  document.querySelectorAll("[data-ai-control]").forEach(initializeAIControl);
  document.querySelectorAll("[data-release-candidate]").forEach(initializeReleaseCandidate);
  updateReviewContinue();
  initializeSnapshotStreams();
  const runPanel = document.getElementById("run-panel");
  if (runPanel) window.requestAnimationFrame(() => locateCurrentStage(runPanel, "auto"));
})();
