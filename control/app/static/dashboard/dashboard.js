"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const state = { token: "", snapshot: null, runId: "", scope: "run", selectedTask: null,
    controller: null, reconnectTimer: null, seenFiles: new Set(), firstSnapshot: true,
    newFiles: new Set(), keys: {}, connected: false, lastByte: 0 };
  const labels = { RUNNING: "Đang thực hiện", ASSIGNED: "Đã giao việc", COMPLETED: "Hoàn thành",
    IDLE: "Sẵn sàng", QUEUED: "Trong kế hoạch", RETRY_WAIT: "Đang thử lại", BLOCKED: "Đang bị chặn",
    FAILED: "Có lỗi", CANCELLED: "Đã hủy", OFFLINE: "Mất kết nối", UNREGISTERED: "Chưa kết nối",
    PLANNING: "Đang lập kế hoạch", CREATED: "Vừa tạo", AWAITING_APPROVAL: "Chờ phê duyệt", LOCAL: "Chạy trên Control",
    WAITING_WORKER: "Chờ worker" };
  const eventLabels = { ARTIFACT_RECEIVED: "Đã nhận file bàn giao", ARTIFACT_RESTORED: "Đã khôi phục file",
    TASK_DISPATCHED: "Đã giao công việc", TASK_STARTED: "Bắt đầu thực hiện", TASK_COMPLETED: "Hoàn thành công việc",
    TASK_FAILED: "Tác vụ có lỗi", TASK_BLOCKED: "Tác vụ đang chờ xử lý", WORKER_ONLINE: "Máy đã kết nối",
    WORKER_OFFLINE: "Máy mất kết nối", PROJECT_AUTO_RECOVERY: "PM đang khôi phục công việc",
    TASK_RECOVERY_SCHEDULED: "PM đã lên lịch khôi phục", PM_RECOVERY_COMPLETED: "PM đã hoàn thành phần làm thay",
    RELEASE_ACCEPTED: "Đã nghiệm thu phiên bản", RUN_CREATED: "Đã tạo dự án", PLAN_SAVED: "Đã lưu kế hoạch" };
  const active = (status) => ["RUNNING", "ASSIGNED"].includes(status);
  const taskState = (task) => active(task.status) && ["OFFLINE", "UNREGISTERED"].includes(task.worker_connection) ? "WAITING_WORKER" : task.status;
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const number = (value) => new Intl.NumberFormat("vi-VN").format(value || 0);
  function ago(value) {
    if (!value) return "Chưa có dữ liệu";
    const seconds = Math.max(0, (Date.now() - Date.parse(value)) / 1000);
    if (!Number.isFinite(seconds)) return "—";
    if (seconds < 10) return "Vừa xong";
    if (seconds < 60) return `${Math.floor(seconds)} giây trước`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} phút trước`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} giờ trước`;
    return new Date(value).toLocaleDateString("vi-VN");
  }
  function bytes(value) {
    if (value == null) return "—";
    if (value < 1024) return `${value} B`;
    if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / 1024 ** 2).toFixed(1)} MB`;
  }
  function badge(status) {
    const cls = status === "COMPLETED" || status === "STORED" ? "good" : active(status) ? "active" :
      ["FAILED", "BLOCKED", "MISSING"].includes(status) ? "bad" : ["RETRY_WAIT", "AWAITING_APPROVAL", "PLANNING", "WAITING_WORKER"].includes(status) ? "warn" : "";
    return `<span class="badge ${cls}">${escape(labels[status] || ({ STORED: "Đã lưu", MISSING: "Thiếu file", REFERENCE: "Tham chiếu" }[status]) || status)}</span>`;
  }
  function updateIfChanged(key, data, render) {
    const serialized = JSON.stringify(data);
    if (serialized !== state.keys[key]) { state.keys[key] = serialized; render(); }
  }
  function connection(online, message) {
    state.connected = online;
    $("live-status").classList.toggle("offline", !online);
    $("live-status").innerHTML = `<i class="dot"></i>${escape(message || (online ? "Trực tiếp" : "Đang kết nối lại"))}`;
    $("side-connection").innerHTML = `<i class="dot ${online ? "" : "muted"}"></i> ${online ? "Luồng dữ liệu hoạt động" : "Đang chờ kết nối"}`;
  }
  function alert(message) { $("connection-alert").textContent = message; $("connection-alert").hidden = !message; }
  function toast(message) {
    const node = document.createElement("div"); node.className = "toast"; node.textContent = message;
    $("toasts").append(node); setTimeout(() => node.remove(), 6000);
    while ($("toasts").children.length > 3) $("toasts").firstElementChild.remove();
  }
  function accept(data) {
    const previousRun = state.snapshot?.selected_run?.run_id;
    state.snapshot = data;
    for (const file of data.recent_artifacts) {
      if (!state.firstSnapshot && !state.seenFiles.has(file.artifact_id)) state.newFiles.add(file.artifact_id);
      state.seenFiles.add(file.artifact_id);
    }
    if (state.newFiles.size) {
      const newFiles = data.recent_artifacts.filter((f) => state.newFiles.has(f.artifact_id));
      if (newFiles.length && !state.firstSnapshot) {
        const first = newFiles[0];
        toast(`↓ ${first.created_by} đã gửi ${first.name}${newFiles.length > 1 ? ` và ${newFiles.length - 1} file khác` : ""}.`);
      }
    }
    state.firstSnapshot = false;
    if (previousRun !== data.selected_run?.run_id) { state.selectedTask = null; $("task-dialog").close(); }
    connection(true); alert("");
    $("last-updated").textContent = `Đồng bộ lúc ${new Date(data.generated_at).toLocaleTimeString("vi-VN")}`;
    $("side-host").textContent = data.host.name;
    $("storage-host").textContent = `Lưu trên ${data.host.name} · Control Node`;
    $("storage-root").textContent = data.host.artifact_root;
    $("footer-revision").textContent = `Bản dữ liệu ${data.revision.slice(0, 8)}`;
    $("metric-agents").textContent = number(data.summary.active_agents);
    $("metric-agents-total").textContent = `/ ${data.summary.agents} agent`;
    $("metric-workers").textContent = number(data.summary.online_workers);
    $("metric-workers-total").textContent = `/ ${data.summary.workers} máy`;
    $("metric-files").textContent = number(data.summary.files_received);
    $("metric-progress").textContent = data.selected_run ? data.selected_run.progress : "—";
    $("metric-tasks").textContent = data.selected_run ? `${data.selected_run.task_counts.COMPLETED} / ${data.selected_run.task_total} task hoàn thành` : "Chưa có dự án";
    $("side-file-count").textContent = number(data.summary.files_received);
    $("agent-count").textContent = data.agents.length;
    updateIfChanged("agents", data.agents, renderAgents);
    updateIfChanged("workers", data.workers, () => {
      $("machine-strip").innerHTML = data.workers.map((w) => `<span title="${escape(w.last_seen ? `Heartbeat: ${w.last_seen}` : "Chưa đăng ký worker")}"><i class="dot ${w.status === "ONLINE" ? "" : "offline-dot"}"></i>${escape(w.machine_id)} · ${escape(labels[w.status] || w.status)}</span>`).join("");
    });
    updateIfChanged("runs", data.runs, () => {
      $("run-select").innerHTML = data.runs.length ? data.runs.map((r) => `<option value="${escape(r.run_id)}">${escape(r.name)} · ${escape(r.run_id)}</option>`).join("") : '<option value="">Chưa có dự án</option>';
    });
    if (data.selected_run) $("run-select").value = data.selected_run.run_id;
    updateIfChanged("run", data.selected_run, renderRun);
    updateIfChanged("graph", [data.tasks, data.edges], renderGraph);
    updateIfChanged("files", [data.artifacts, data.recent_artifacts], renderFiles);
    updateIfChanged("activity", data.activity, renderActivity);
    if ($("task-dialog").open && state.selectedTask) renderTaskDetail();
    state.newFiles.clear();
  }
  function renderAgents() {
    $("agent-grid").innerHTML = state.snapshot.agents.map((agent) => {
      const task = agent.task;
      const working = !!task && active(task.status) && ["ONLINE", "LOCAL"].includes(agent.connection);
      const description = task?.title || (agent.connection === "OFFLINE" ? "Chờ máy trực tuyến để tiếp tục" : agent.connection === "UNREGISTERED" ? "Chưa nhận heartbeat từ worker" : "Sẵn sàng nhận công việc tiếp theo");
      return `<article class="agent-card ${working ? "working" : ""}"><div class="agent-top"><span class="role-icon">${escape(agent.role === "SCRUM_MASTER" ? "SM" : agent.role === "SECURITY" ? "SEC" : agent.role)}</span><span class="agent-machine">${escape(agent.connection === "LOCAL" ? "CONTROL" : agent.machine_id)}</span></div><div class="agent-name">${escape(agent.role.replace("_", " "))}</div><p class="agent-task" title="${escape(description)}">${escape(description)}</p><div class="agent-bottom"><span class="${working ? "running" : agent.connection === "OFFLINE" ? "offline-text" : ""}"><i class="dot ${working ? "" : "muted"}"></i>${escape(labels[agent.status] || agent.status)}</span><span class="agent-id" title="${escape(agent.agent_id)}">${escape(agent.agent_id)}</span></div></article>`;
    }).join("");
    updateIfChanged("agent-filter", state.snapshot.agents.map((a) => a.agent_id), () => {
      const selected = $("file-agent-filter").value;
      $("file-agent-filter").innerHTML = '<option value="">Tất cả agent</option>' + state.snapshot.agents.map((a) => `<option value="${escape(a.agent_id)}">${escape(a.agent_id)}</option>`).join("");
      $("file-agent-filter").value = selected;
    });
  }
  function renderRun() {
    const run = state.snapshot.selected_run;
    $("run-summary").innerHTML = run ? `<div class="run-info"><h3>${escape(run.name)} ${badge(run.status)}<code>${escape(run.run_id)}</code></h3><p title="${escape(run.brief)}">${escape(run.brief)}</p></div><div class="run-progress"><div><span>${run.task_counts.COMPLETED}/${run.task_total} task</span><strong>${run.progress}%</strong></div><progress max="100" value="${run.progress}" aria-label="Tiến độ dự án">${run.progress}%</progress></div>` : '<p class="empty">Chưa có dự án. Kế hoạch sẽ xuất hiện khi được tạo trên Control Node.</p>';
    $("recovery-banner").hidden = !run?.recovery;
    if (run?.recovery) $("recovery-banner").textContent = `↻ PM đang xử lý: ${run.recovery.reason} · Lần ${run.recovery.count} · Thử lại lúc ${new Date(run.recovery.next_retry_at).toLocaleTimeString("vi-VN")}`;
  }
  function orderedTasks(tasks, edges) {
    const incoming = new Map(tasks.map((task) => [task.task_id, 0])), outgoing = new Map();
    const byId = new Map(tasks.map((task) => [task.task_id, task]));
    for (const edge of edges) if (byId.has(edge.from) && byId.has(edge.to)) {
      incoming.set(edge.to, incoming.get(edge.to) + 1);
      outgoing.set(edge.from, [...(outgoing.get(edge.from) || []), edge.to]);
    }
    const ready = tasks.filter((task) => incoming.get(task.task_id) === 0), result = [];
    while (ready.length) {
      const task = ready.shift(); result.push(task);
      for (const id of outgoing.get(task.task_id) || []) {
        incoming.set(id, incoming.get(id) - 1); if (!incoming.get(id)) ready.push(byId.get(id));
      }
    }
    return result.length === tasks.length ? result : tasks;
  }
  function renderGraph() {
    const { tasks, edges } = state.snapshot;
    $("graph-empty").hidden = !!tasks.length;
    $("graph-canvas").hidden = !tasks.length;
    $("task-nodes").innerHTML = orderedTasks(tasks, edges).map((task) => `<button class="task-node ${active(taskState(task)) ? "running" : task.status === "COMPLETED" ? "done" : ["FAILED", "BLOCKED"].includes(task.status) ? "failed" : ""} ${task.task_id === state.selectedTask ? "selected" : ""}" data-task-id="${escape(task.task_id)}"><span class="node-top"><span class="node-role">${escape(task.role)}</span>${badge(taskState(task))}</span><span class="node-title">${escape(task.title)}</span><span class="node-module">${escape(task.module_name || task.module_id || task.epic || "Toàn dự án")}</span></button>`).join("");
    requestAnimationFrame(drawEdges);
  }
  function drawEdges() {
    const canvas = $("graph-canvas"), svg = $("graph-edges");
    if (!state.snapshot || canvas.hidden) return;
    const rect = canvas.getBoundingClientRect();
    svg.setAttribute("viewBox", `0 0 ${rect.width} ${rect.height}`);
    svg.innerHTML = '<defs><marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0 0L8 4L0 8" fill="#bdcfd7"/></marker></defs>';
    const nodes = new Map([...$("task-nodes").children].map((node) => [node.dataset.taskId, node]));
    for (const edge of state.snapshot.edges) {
      const from = nodes.get(edge.from), to = nodes.get(edge.to); if (!from || !to) continue;
      const a = from.getBoundingClientRect(), b = to.getBoundingClientRect();
      const sameRow = Math.abs(a.top - b.top) < 8;
      const x1 = (sameRow ? a.right : a.left + a.width / 2) - rect.left;
      const y1 = (sameRow ? a.top + a.height / 2 : a.bottom) - rect.top;
      const x2 = (sameRow ? b.left : b.left + b.width / 2) - rect.left;
      const y2 = (sameRow ? b.top + b.height / 2 : b.top) - rect.top;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", sameRow ? `M${x1},${y1} C${x1 + 13},${y1} ${x2 - 13},${y2} ${x2 - 3},${y2}` : `M${x1},${y1} C${x1},${y1 + 19} ${x2},${y2 - 19} ${x2},${y2 - 3}`);
      path.setAttribute("class", `edge${[edge.from, edge.to].includes(state.selectedTask) ? " highlight" : ""}`);
      path.setAttribute("marker-end", "url(#arrow)"); svg.append(path);
    }
  }
  function currentFiles() { return state.snapshot ? (state.scope === "all" ? state.snapshot.recent_artifacts : state.snapshot.artifacts) : []; }
  function renderFiles() {
    const query = $("file-search").value.toLocaleLowerCase(), agent = $("file-agent-filter").value;
    const all = currentFiles();
    const files = all.filter((file) => (!agent || file.created_by === agent) &&
      [file.name, file.created_by, file.task_title, file.task_id, file.local_path, file.type].join(" ").toLocaleLowerCase().includes(query));
    $("file-count").textContent = all.length;
    $("file-result-count").textContent = `${files.length} / ${all.length} file đang hiển thị`;
    $("files-empty").hidden = !!files.length;
    $("files-empty").textContent = all.length ? "Không có file khớp bộ lọc." : "Chưa nhận được file bàn giao trong phạm vi này.";
    $("file-rows").innerHTML = files.map((file) => {
      const extension = file.name.split(".").pop().slice(0, 5);
      return `<tr class="file-row ${state.newFiles.has(file.artifact_id) ? "new-file" : ""}"><td><div class="file-name"><span class="file-symbol">${escape(extension)}</span><div><strong>${escape(file.name)}</strong><small>${escape(file.type)} · ${bytes(file.size_bytes)}</small></div></div></td><td><span class="file-agent"><i class="dot"></i>${escape(file.created_by)}</span><small>${escape(file.source_machine || "Control Node")}${file.recovered_by_pm ? " · PM làm thay" : ""}</small></td><td class="file-task">${escape(file.task_title || file.task_id || "—")}<small>${escape(file.module_id || "Toàn dự án")}</small></td><td><div class="path-wrap"><code class="file-path">${escape(file.local_path || file.uri)}</code><button class="copy-button" data-copy="${escape(file.artifact_id)}" title="Sao chép đường dẫn" aria-label="Sao chép đường dẫn ${escape(file.name)}">⧉</button></div>${file.available ? "" : badge(file.storage_status)}</td><td class="file-time"><time data-time="${escape(file.received_at)}" title="${escape(file.received_at)}">${ago(file.received_at)}</time></td><td><button class="download-button" data-download="${escape(file.artifact_id)}" ${file.available ? "" : "disabled"} title="Tải file" aria-label="Tải ${escape(file.name)}">↓</button></td></tr>`;
    }).join("");
  }
  function renderActivity() {
    const rows = state.snapshot.activity;
    $("activity-list").innerHTML = rows.length ? rows.map((event) => `<div class="activity-row"><span class="activity-icon">${event.type.includes("ARTIFACT") ? "↓" : event.type.includes("RECOVER") ? "↻" : "·"}</span><div><strong>${escape(eventLabels[event.type] || event.type)}</strong><p>${escape(event.actor || "Hệ thống")}${event.target ? " · " + escape(event.target) : ""}</p></div><time data-time="${escape(event.at)}" title="${escape(event.at)}">${ago(event.at)}</time></div>`).join("") : '<p class="empty">Chưa có sự kiện được ghi nhận.</p>';
  }
  function openTask(id) {
    state.selectedTask = id; renderTaskDetail(); renderGraph();
    if (!$("task-dialog").open) $("task-dialog").showModal();
  }
  function renderTaskDetail() {
    const task = state.snapshot.tasks.find((t) => t.task_id === state.selectedTask); if (!task) return;
    const predecessors = state.snapshot.edges.filter((edge) => edge.to === task.task_id).map((edge) => state.snapshot.tasks.find((t) => t.task_id === edge.from)).filter(Boolean);
    const outputs = state.snapshot.artifacts.filter((file) => file.task_id === task.task_id);
    const inputs = Object.entries(task.input_artifact_sources).flatMap(([kind, ids]) => ids.map((id) => ({ kind, id })));
    const baseline = Object.entries(task.baseline_artifact_ids).flatMap(([kind, ids]) => ids.map((id) => ({ kind, id })));
    $("task-detail").innerHTML = `${badge(taskState(task))}<h2>${escape(task.title)}</h2><div class="detail-id">${escape(task.task_id)}</div><dl class="detail-meta"><div><dt>Agent phụ trách</dt><dd>${escape(task.agent_id)}</dd></div><div><dt>Module</dt><dd>${escape(task.module_name || task.module_id || "Toàn dự án")}</dd></div><div><dt>Action</dt><dd>${escape(task.action)}</dd></div><div><dt>Số lượt thực hiện</dt><dd>${task.attempts}${task.recovery_count ? ` · Khôi phục ${task.recovery_count} lần` : ""}</dd></div></dl>${task.original_role ? `<div class="recovery-banner">PM khôi phục công việc ban đầu của ${escape(task.original_role)}.</div>` : ""}<section class="detail-section"><h3>Chỉ dẫn công việc</h3><p>${escape(task.instruction)}</p></section>${task.acceptance_criteria.length ? `<section class="detail-section"><h3>Tiêu chí nghiệm thu</h3><ul>${task.acceptance_criteria.map((item) => `<li>${escape(item)}</li>`).join("")}</ul></section>` : ""}${task.summary || task.error ? `<section class="detail-section"><h3>Kết quả / tình trạng</h3><p>${escape(task.error || task.summary)}</p></section>` : ""}<section class="detail-section"><h3>Phụ thuộc vào ${predecessors.length} task</h3>${predecessors.map((item) => `<button class="detail-link" data-task-id="${escape(item.task_id)}">${escape(item.role)} · ${escape(item.title)} ↗</button>`).join("") || '<p>Không có task phụ thuộc trước.</p>'}</section><section class="detail-section"><h3>Nguồn tài liệu đầu vào</h3>${[...inputs, ...baseline].map((item) => `<div class="detail-file"><strong>${escape(item.kind)}</strong><code>${escape(item.id)}</code></div>`).join("") || '<p>Không yêu cầu artifact đầu vào.</p>'}</section><section class="detail-section"><h3>File đã bàn giao (${outputs.length})</h3>${outputs.map((file) => `<div class="detail-file"><strong>${escape(file.name)} · ${escape(file.created_by)}</strong><code>${escape(file.local_path || file.uri)}</code><button class="text-button" data-copy="${escape(file.artifact_id)}">Sao chép đường dẫn ⧉</button></div>`).join("") || '<p>Chưa có file được lưu tại Control Node.</p>'}</section>`;
  }
  async function copyPath(file) {
    const path = file.local_path || file.uri;
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(path);
      else {
        const input = document.createElement("textarea"); input.value = path; input.className = "sr-only";
        ($("task-dialog").open ? $("task-dialog") : document.body).append(input); input.select();
        const copied = document.execCommand("copy"); input.remove(); if (!copied) throw new Error("Clipboard unavailable");
      }
      toast("Đã sao chép đường dẫn trên Control Node.");
    } catch { toast(`Đường dẫn: ${path}`); }
  }
  async function downloadFile(file, button) {
    button.disabled = true;
    try {
      const response = await fetch(file.download_url, { headers: { Authorization: `Bearer ${state.token}` }, cache: "no-store" });
      if (!response.ok) throw new Error(response.status === 409 ? "File không khớp checksum. PM cần khôi phục trước khi tải." : "Không tải được file. Hãy kiểm tra file còn trên Control Node.");
      const url = URL.createObjectURL(await response.blob()), anchor = document.createElement("a");
      anchor.href = url; anchor.download = file.name; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
  }
  function stopStream() {
    clearTimeout(state.reconnectTimer); state.reconnectTimer = null;
    if (state.controller) state.controller.abort(); state.controller = null;
  }
  async function startStream() {
    stopStream(); if (!state.token) return;
    if (navigator.onLine === false) {
      connection(false, "Thiết bị mất mạng"); alert("Thiết bị đang mất kết nối mạng. Dashboard sẽ tự kết nối lại khi có mạng.");
      state.reconnectTimer = setTimeout(startStream, 2000); return;
    }
    const controller = new AbortController(); state.controller = controller; state.lastByte = Date.now();
    const query = state.runId ? `?run_id=${encodeURIComponent(state.runId)}` : "";
    try {
      const response = await fetch(`/api/v1/dashboard/stream${query}`, { headers: { Authorization: `Bearer ${state.token}`, Accept: "text/event-stream" }, signal: controller.signal, cache: "no-store" });
      if ([401, 403].includes(response.status)) { disconnect("Mã truy cập không còn hợp lệ. Vui lòng kết nối lại."); return; }
      if (!response.ok || !response.body) throw new Error("Stream unavailable");
      const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = "";
      while (true) {
        const { done, value } = await reader.read(); if (done) break;
        state.lastByte = Date.now(); buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
          const lines = block.split("\n"), event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
          const data = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
          if (event === "snapshot" && data) accept(JSON.parse(data));
          if (event === "unavailable") {
            if (data && JSON.parse(data).message === "Run not found") { state.runId = ""; controller.abort(); }
            connection(false, "Dữ liệu gián đoạn"); alert("Control Node đang chờ dữ liệu. Thông tin bên dưới là lần đồng bộ gần nhất.");
          }
        }
      }
      throw new Error("Stream ended");
    } catch (error) {
      if (state.controller !== controller || !state.token) return;
      connection(false); alert("Mất luồng realtime. Dashboard đang tự kết nối lại; dữ liệu hiện tại có thể đã cũ.");
      state.reconnectTimer = setTimeout(startStream, 2000);
    }
  }
  function disconnect(message = "") {
    stopStream(); state.token = ""; state.snapshot = null; state.keys = {}; state.selectedTask = null;
    state.firstSnapshot = true; state.seenFiles.clear(); state.newFiles.clear(); state.runId = "";
    for (const id of ["agent-grid", "machine-strip", "task-nodes", "file-rows", "activity-list", "task-detail", "run-summary", "graph-edges"]) $(id).replaceChildren();
    for (const id of ["metric-agents", "metric-workers", "metric-progress", "metric-files"]) $(id).textContent = "—";
    $("storage-root").textContent = "Chưa có dữ liệu"; $("side-host").textContent = "Chưa kết nối";
    $("storage-host").textContent = "Kho file trên Control Node";
    $("run-select").innerHTML = '<option value="">Chưa có dự án</option>';
    $("metric-tasks").textContent = "Chưa chọn dự án";
    $("footer-revision").textContent = "Chưa đồng bộ";
    connection(false, "Chưa kết nối"); $("task-dialog").close();
    $("app").inert = true; $("auth-layer").hidden = false; $("access-token").value = "";
    $("auth-error").textContent = message; $("auth-error").hidden = !message; $("access-token").focus();
  }
  $("auth-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const token = $("access-token").value.trim(); if (!token) return;
    $("connect-button").disabled = true; $("auth-error").hidden = true;
    try {
      const response = await fetch("/api/v1/dashboard/snapshot", { headers: { Authorization: `Bearer ${token}` }, cache: "no-store" });
      if (!response.ok) throw new Error([401, 403].includes(response.status) ? "Cần ADMIN_TOKEN hợp lệ để xem dashboard." : "Control Node chưa đọc được dữ liệu. Kiểm tra kết nối MySQL rồi thử lại.");
      const data = await response.json(); state.token = token; $("access-token").value = "";
      $("auth-layer").hidden = true; $("app").inert = false; accept(data); startStream();
    } catch (error) { $("auth-error").textContent = error.message; $("auth-error").hidden = false; }
    finally { $("connect-button").disabled = false; }
  });
  $("disconnect").addEventListener("click", () => disconnect());
  $("run-select").addEventListener("change", (event) => { state.runId = event.target.value; connection(false, "Đang chuyển dự án"); startStream(); });
  document.querySelectorAll("[data-scope]").forEach((button) => button.addEventListener("click", () => {
    state.scope = button.dataset.scope;
    document.querySelectorAll("[data-scope]").forEach((item) => { item.classList.toggle("active", item === button); item.setAttribute("aria-pressed", String(item === button)); });
    renderFiles();
  }));
  $("file-search").addEventListener("input", renderFiles); $("file-agent-filter").addEventListener("change", renderFiles);
  document.addEventListener("click", (event) => {
    const task = event.target.closest("[data-task-id]"); if (task) { openTask(task.dataset.taskId); return; }
    const button = event.target.closest("[data-copy], [data-download]"); if (!button || !state.snapshot) return;
    const id = button.dataset.copy || button.dataset.download;
    const file = [...state.snapshot.artifacts, ...state.snapshot.recent_artifacts].find((item) => item.artifact_id === id);
    if (file) button.dataset.copy ? copyPath(file) : downloadFile(file, button);
  });
  $("close-task").addEventListener("click", () => $("task-dialog").close());
  $("focus-active").addEventListener("click", () => {
    const task = state.snapshot?.tasks.find((item) => active(item.status)) || state.snapshot?.tasks.find((item) => ["BLOCKED", "FAILED"].includes(item.status));
    const node = [...$("task-nodes").children].find((item) => item.dataset.taskId === task?.task_id) || $("task-nodes").firstElementChild;
    if (node) { node.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" }); node.focus({ preventScroll: true }); }
  });
  document.querySelectorAll(".nav-item").forEach((link) => link.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item === link));
  }));
  new ResizeObserver(() => requestAnimationFrame(drawEdges)).observe($("graph-canvas"));
  setInterval(() => {
    $("clock").textContent = new Date().toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    document.querySelectorAll("[data-time]").forEach((node) => { node.textContent = ago(node.dataset.time); });
    if (state.token && state.controller && Date.now() - state.lastByte > 12000) state.controller.abort();
  }, 1000);
  window.addEventListener("pagehide", stopStream);
  window.addEventListener("offline", () => { if (state.token) startStream(); });
  window.addEventListener("online", () => { if (state.token) startStream(); });
})();
