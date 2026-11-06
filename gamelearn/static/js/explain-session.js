(() => {
  const createButton = document.querySelector("#explain-session-button");
  const stopButton = document.querySelector("#stop-explanation-button");
  const feedback = document.querySelector("#explain-session-feedback");
  const progress = document.querySelector("#explain-progress");
  const progressBar = document.querySelector("#explain-progress-bar");
  const elapsed = document.querySelector("#explain-elapsed");
  const telemetry = document.querySelector("#explain-telemetry");
  const activityCount = document.querySelector("#explain-activity-count");
  const lastActivity = document.querySelector("#explain-last-activity");
  const tokenUsage = document.querySelector("#explain-token-usage");
  const activityLog = document.querySelector("#explain-activity-log");
  const model = document.querySelector("#explain-model");
  const reasoning = document.querySelector("#explain-reasoning");
  const modelSelect = document.querySelector("#learning-model");
  const reasoningSelect = document.querySelector("#learning-reasoning");
  if (!createButton || !stopButton || !feedback || !progress || !progressBar || !elapsed || !telemetry || !activityCount || !lastActivity || !tokenUsage || !activityLog) return;

  const activeStatuses = new Set(["QUEUED", "RUNNING", "STOPPING"]);
  const terminalStatuses = new Set(["COMPLETED", "FAILED", "CANCELLED"]);
  let pollTimer = null;
  let elapsedTimer = null;
  let startedAt = null;
  let latestJob = null;
  let latestJobReceivedAt = null;

  function updateReasoningChoices() {
    if (!modelSelect || !reasoningSelect) return;
    const efforts = JSON.parse(modelSelect.selectedOptions[0].dataset.efforts);
    const previous = reasoningSelect.value;
    reasoningSelect.replaceChildren(...efforts.map((effort) => new Option(effort[0].toUpperCase() + effort.slice(1), effort)));
    reasoningSelect.value = efforts.includes(previous) ? previous : efforts.includes("low") ? "low" : efforts[0];
  }
  modelSelect?.addEventListener("change", updateReasoningChoices);
  updateReasoningChoices();

  const formatNumber = (value) => new Intl.NumberFormat().format(Number(value || 0));

  async function readJson(response) {
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "GameLearn could not contact Codex.");
    return payload;
  }

  function showProgress(percent) {
    progress.classList.remove("d-none");
    progress.setAttribute("aria-hidden", "false");
    progressBar.classList.remove("bg-danger", "bg-warning");
    progressBar.classList.add("progress-bar-animated");
    progressBar.style.width = `${percent}%`;
    progressBar.setAttribute("aria-valuenow", String(percent));
    if (!startedAt) startedAt = Date.now();
    if (!elapsedTimer) {
      elapsedTimer = window.setInterval(() => {
        elapsed.textContent = `${Math.floor((Date.now() - startedAt) / 1000)}s`;
        updateLiveTelemetry();
      }, 1000);
    }
  }

  function currentActivityAge() {
    if (!latestJob || latestJob.last_activity_seconds === undefined || !latestJobReceivedAt) return null;
    return latestJob.last_activity_seconds + Math.floor((Date.now() - latestJobReceivedAt) / 1000);
  }

  function updateLiveTelemetry() {
    if (!latestJob) return;
    const age = currentActivityAge();
    if (age === null) {
      lastActivity.textContent = "Waiting for the first CLI event";
    } else {
      lastActivity.textContent = `Last CLI activity ${age}s ago`;
    }
    feedback.textContent = latestJob.message;
    if (activeStatuses.has(latestJob.status) && age !== null && age >= 20) {
      feedback.textContent = `${latestJob.message} No new CLI event for ${age}s; the Codex process is still running.`;
    }
  }

  function renderTelemetry(job) {
    if (model) model.textContent = job.model || "Waiting for Codex to confirm";
    if (reasoning) reasoning.textContent = job.reasoning_effort ? job.reasoning_effort[0].toUpperCase() + job.reasoning_effort.slice(1) : "Unavailable";
    telemetry.classList.toggle("d-none", job.status === "NOT_STARTED");
    activityCount.textContent = job.activity_count
      ? `${job.activity_count} safe CLI ${job.activity_count === 1 ? "update" : "updates"}`
      : "Waiting for the first CLI event";
    const usage = job.token_usage;
    tokenUsage.textContent = usage
      ? `Tokens: ${formatNumber(usage.inputTokens)} input (${formatNumber(usage.cachedInputTokens)} cached) · ${formatNumber(usage.outputTokens)} output`
      : `Estimated starting context: about ${formatNumber(job.estimated_input_tokens)} tokens`;
    const entries = Array.isArray(job.activity_log) ? job.activity_log : [];
    activityLog.replaceChildren(...entries.map((entry) => {
      const item = document.createElement("li");
      item.value = entry.sequence;
      item.textContent = entry.message;
      return item;
    }));
    latestJob = job;
    latestJobReceivedAt = Date.now();
    updateLiveTelemetry();
  }

  function finishProgress(outcome) {
    if (elapsedTimer) window.clearInterval(elapsedTimer);
    elapsedTimer = null;
    progressBar.classList.remove("progress-bar-animated", "bg-danger", "bg-warning");
    progressBar.style.width = "100%";
    progressBar.setAttribute("aria-valuenow", "100");
    if (outcome === "failed") progressBar.classList.add("bg-danger");
    if (outcome === "cancelled") progressBar.classList.add("bg-warning");
  }

  function updateControls(status) {
    const active = activeStatuses.has(status);
    createButton.disabled = active;
    if (modelSelect) modelSelect.disabled = active;
    if (reasoningSelect) reasoningSelect.disabled = active;
    stopButton.classList.toggle("d-none", !active);
    stopButton.disabled = status === "STOPPING";
    stopButton.textContent = status === "STOPPING" ? "Stopping…" : "Stop";
  }

  function redirectToLearningPage(job) {
    const destination = job.learning_page_url || createButton.dataset.learningPageUrl;
    if (destination) window.location.assign(destination);
  }

  function renderJob(job) {
    renderTelemetry(job);
    updateControls(job.status);
    if (job.status === "QUEUED") showProgress(job.progress_percent || 12);
    if (job.status === "RUNNING") showProgress(job.progress_percent || 28);
    if (job.status === "STOPPING") showProgress(85);
    if (job.status === "COMPLETED") {
      finishProgress("completed");
      redirectToLearningPage(job);
    }
    if (job.status === "FAILED") finishProgress("failed");
    if (job.status === "CANCELLED") finishProgress("cancelled");
  }

  function schedulePoll(delay = 1500) {
    if (pollTimer) window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(pollStatus, delay);
  }

  async function pollStatus() {
    pollTimer = null;
    try {
      const job = await readJson(await fetch(createButton.dataset.statusUrl, {cache: "no-store"}));
      renderJob(job);
      if (!terminalStatuses.has(job.status)) schedulePoll();
    } catch (error) {
      feedback.textContent = error.message;
      finishProgress("failed");
      updateControls("FAILED");
    }
  }

  createButton.addEventListener("click", async () => {
    if (pollTimer) window.clearTimeout(pollTimer);
    pollTimer = null;
    updateControls("QUEUED");
    feedback.textContent = "Starting learning-page creation in the background…";
    startedAt = Date.now();
    showProgress(8);
    try {
      const job = await readJson(await fetch(createButton.dataset.explainUrl, {
        method: "POST",
        headers: {"X-GameLearn-Token": createButton.dataset.csrfToken, "Content-Type": "application/json"},
        body: JSON.stringify({model: modelSelect.value, reasoning_effort: reasoningSelect.value}),
      }));
      renderJob(job);
      if (!terminalStatuses.has(job.status)) schedulePoll(500);
    } catch (error) {
      feedback.textContent = error.message;
      finishProgress("failed");
      updateControls("FAILED");
    }
  });

  stopButton.addEventListener("click", async () => {
    stopButton.disabled = true;
    stopButton.textContent = "Stopping…";
    feedback.textContent = "Stopping learning-page creation…";
    try {
      const job = await readJson(await fetch(createButton.dataset.stopUrl, {
        method: "POST",
        headers: {"X-GameLearn-Token": createButton.dataset.csrfToken},
      }));
      renderJob(job);
      if (!terminalStatuses.has(job.status)) schedulePoll(300);
    } catch (error) {
      feedback.textContent = error.message;
      finishProgress("failed");
      updateControls("FAILED");
    }
  });

  // Restore an in-flight job when the summary page is revisited.
  fetch(createButton.dataset.statusUrl, {cache: "no-store"})
    .then(readJson)
    .then((job) => {
      if (job.status === "NOT_STARTED") return;
      if (job.status === "COMPLETED") {
        feedback.textContent = "The learning page is ready. Recreate it to apply the latest learning format.";
        updateControls("COMPLETED");
        return;
      }
      renderJob(job);
      if (activeStatuses.has(job.status)) schedulePoll(500);
    })
    .catch(() => {});
})();
