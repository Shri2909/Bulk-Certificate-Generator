"use strict";

const $ = (id) => document.getElementById(id);
const JOBS = "/api/v1/certificate-jobs";
const terminal = (job) => job && ["completed", "completed_with_errors", "failed"].includes(job.status);
const sample = () => [
  ["Zoë Łukasz", "zoe@example.com"], ["Νίκος Παπαδόπουλος", "nikos@example.org"],
  ["Екатерина Иванова", "ekaterina@example.org"], ["Seán O'Connor-Smith", "sean@example.ie"],
  ["Bob Martin", "not-an-email"], ["李小龙", "bruce@example.com"],
  ["Zoe Again", " ZOE@example.com "], ["Anne-Marie Lévesque", "anne@example.ca"],
  ["Mohammed Al-Rashid", "mohammed@example.com"], ["Priya Raman", "priya@example.in"],
  ["Ngọc Trần", "ngoc@example.vn"], ["Lars Øvergaard", "lars@example.no"],
].map(([name, email]) => ({ name, email }));

const chapters = [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 4];
const steps = [
  {
    title: "Good data in. Clear errors out.",
    copy: "A real list is rarely perfect. This sample includes a bad email, a duplicate, and a name the template font cannot render. Let’s check it before creating anything.",
    notice: "Validation runs independently for each recipient. One bad row doesn’t block everyone else.",
    action: "Validate this batch", caption: "Dry run · no job or PDFs will be created", node: "input",
    note: "Start with the problem: creating certificates one at a time is tedious. The names and emails are editable. The default sample has 12 rows: 9 valid and 3 rejected. Different scripts expose the font support boundary clearly.",
  },
  {
    title: "Find mistakes before making files.",
    copy: "The dry run has checked every row. Rejected rows explain what to fix. Valid recipients can continue together in one request.",
    notice: "A dry run returns a report without saving a job, reserving a key, or generating any PDFs.",
    action: "Submit this batch", caption: "POST the batch · receive 202 Accepted", node: "validate",
    note: "Point to each row error. This is partial acceptance: the real submission will store the rejected rows for reporting and queue the valid ones. A whole-request structural error still rejects the request.",
  },
  {
    title: "Accepted now. Generated next.",
    copy: "The API has saved this job and returned immediately. Valid rows are queued on disk. We’ve held the demo worker offline so you can see this boundary.",
    notice: "The request does not wait for PDFs. SQLite holds the job, its recipients, and their statuses durably.",
    action: "Start generation", caption: "Start a real worker process", node: "queue",
    note: "The queue is the database, not an in-memory list. The normal app starts embedded workers automatically; the demo starts a separate worker so its lifecycle is visible.",
  },
  {
    title: "One certificate at a time.",
    copy: "Watch each valid row move from queued to rendering to ready. The API stays available while a separate worker creates the files.",
    notice: "The worker claims a row, renders outside the database transaction, then saves the PDF and records the result.",
    action: "Explore the output", caption: "Available when this batch finishes", node: "worker",
    note: "Rendering has a deliberate 650 ms delay so the audience can follow the state transitions. This is presentation pacing, not a performance benchmark. Progress is finished valid items divided by all valid items.",
  },
  {
    title: "Real files. Ready to share.",
    copy: "Open a finished certificate below the list. The ZIP contains every successful PDF and a CSV manifest covering every submitted row, including rejections.",
    notice: "The results stay auditable: every row has an outcome, and each successful certificate has its own download link.",
    action: "Replay the same request", caption: "Same payload + same Idempotency-Key", node: "output",
    note: "Open the PDF and zoom in on the recipient, course, date, and certificate ID. Then download the ZIP. Explain that a client may retry if its original network response is lost.",
  },
  {
    title: "Retry the request. Keep one job.",
    copy: "The exact same submission was sent again with the same key. The API returned the original job’s current state instead of creating another batch.",
    notice: "The replay returns HTTP 200 and Idempotent-Replayed: true. A different body with the same key would return 409.",
    action: "Demonstrate a worker crash", caption: "A fresh 6-recipient batch · one real process kill", node: "queue",
    note: "Expand the API inspector to compare the 202 submission with the 200 replay: their job IDs match. Next, the demo queues a separate batch and kills its worker after a claim has committed.",
  },
  {
    title: "The worker stopped. The job stayed.",
    copy: "A real worker was terminated during a claimed render. That row is still processing and the remaining rows are queued. The API and database are still here.",
    notice: "A claim has an 8-second demo lease. After it expires, a replacement worker can reclaim the unfinished item.",
    action: "Start a replacement worker", caption: "Recover from the saved queue", node: "queue",
    note: "This is a real child-process kill, not a changed UI badge. No database rows are edited by the demo controller. If you pause here, the lease may already have expired before you restart the worker.",
  },
  {
    title: "Pick up where it left off.",
    copy: "The replacement worker handles queued recipients and takes over the crashed item after its lease expires. Look for the recovered row and its second attempt.",
    notice: "Recovery keeps the certificate ID. A new claim token prevents a stale worker from recording the result of an old claim.",
    action: "Show an individual failure", caption: "Next: one injected error, three unaffected recipients", node: "worker",
    note: "Point to attempts = 2 and the recovered badge. Execution can happen more than once; the effect is idempotent. This demo proves crash recovery. The stale-token safety case is covered separately by the project tests.",
  },
  {
    title: "One failure. The others finish.",
    copy: "For this chapter only, the demo renderer throws an error for Grace Hopper. Other recipients generate normally. Grace is retried automatically, then marked failed after three attempts.",
    notice: "Rejected means invalid input. Failed means accepted input whose generation could not finish. They need different fixes.",
    action: "Remove the fault & retry", caption: "Replace the demo renderer · requeue failed items only", node: "worker",
    note: "The injected OSError is limited to Grace in the demo worker. This is not an actual disk outage. Watch the three other rows succeed. The next action starts a normal worker and calls the real retry endpoint.",
  },
  {
    title: "Fix the cause. Keep the identity.",
    copy: "The injected fault is gone. The retry endpoint requeued the failed recipient while leaving successful certificates alone. Its certificate ID and download URL remain the same.",
    notice: "Manual retry resets the failed item’s attempt counter. Previously successful recipients do not get generated again.",
    action: "Finish the walkthrough", caption: "Available when the retried certificate is ready", node: "worker",
    note: "Compare the recorded certificate IDs before and after retry in the API inspector. Retry is explicit: it happens after an operator fixes the cause, rather than retrying a broken dependency forever.",
  },
  {
    title: "A whole batch. Every outcome explained.",
    copy: "You’ve seen validation, a durable queue, real PDF output, safe request replay, crash recovery, and a targeted retry. The same production API powered every chapter.",
    notice: "One request gets the work started. Per-recipient status makes the result understandable, even when something goes wrong.",
    action: "Start a new walkthrough", caption: "Existing files remain in the demo session", node: "output",
    note: "Close with the architecture: FastAPI accepts requests, SQLite is the queue and source of truth, and workers use ReportLab to create PDFs on disk. The live demo controls are separate from the production application.",
  },
];

let state = {};
let busy = false;
let server = null;
let job = null;
let items = [];
let pollTimer;
let calls = [];
let selectedCall = 0;
let storageKey;
let firstPoll = true;
let previewUrl = null;
let storageAvailable = true;
let restored = false;

function fresh() {
  return { stage: 0, recipients: sample(), course: "Python for Data Engineering", issuer: "Acme Academy", key: crypto.randomUUID(), jobId: null, originalJobId: null, dry: null, payload: null, crashJobId: null, failureJobId: null, retryIds: null };
}
function save() {
  try { sessionStorage.setItem(storageKey, JSON.stringify(state)); storageAvailable = true; }
  catch { storageAvailable = false; }
  renderSession();
}
function renderSession() {
  if (!server) return;
  $("session-id").textContent = server.session;
  $("session-path").textContent = `data/live-demo/${server.session}/`;
  $("reload-demo").disabled = busy || !storageAvailable;
  $("reload-status").textContent = storageAvailable ? "Same-tab reload recovery" : "Browser storage unavailable";
  $("session-recovery").textContent = storageAvailable
    ? "Reloading this tab restores your walkthrough step and reconnects to the same job. It does not restart the worker or create another job."
    : "This browser has blocked session storage, so this tab cannot restore its walkthrough after a reload.";
  $("restored-state").classList.toggle("hidden", !restored);
}
function escape(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function announce(message) { $("announcement").textContent = message; }
function proof(message, error = false) {
  $("proof").textContent = message;
  $("proof").classList.toggle("hidden", !message);
  $("proof").classList.toggle("error", error);
  if (message) announce(message);
}
function record(call) {
  calls.unshift(call);
  calls = calls.slice(0, 60);
  selectedCall = 0;
  renderCalls();
}
function renderCalls() {
  $("call-count").textContent = `${calls.length} calls`;
  $("call-list").innerHTML = calls.map((call, i) => `<button class="call-entry ${i === selectedCall ? "selected" : ""}" data-call="${i}"><b>${escape(call.method)} ${call.status}</b>${escape(call.label)}<small>${call.duration} ms · ${escape(call.time)}</small></button>`).join("");
  const call = calls[selectedCall];
  if (call) {
    $("call-title").textContent = `${call.method} ${call.path} → ${call.status}`;
    $("call-body").textContent = JSON.stringify({ request: call.request, response_headers: call.headers, response: call.body }, null, 2);
  } else {
    $("call-title").textContent = "Requests and responses will appear here.";
    $("call-body").textContent = "No calls yet.";
  }
}
async function request(path, { method = "GET", body, headers = {}, label = path, log = true } = {}) {
  const start = performance.now();
  const response = await fetch(path, {
    method,
    headers: { ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...(path.startsWith("/demo/") ? { "X-Demo-Control": "local" } : {}), ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(12000), cache: "no-store",
  });
  const data = await response.json();
  if (log) record({ method, path, label, request: { headers, ...(body !== undefined ? { body } : {}) }, body: data, status: response.status, headers: { location: response.headers.get("location"), "idempotent-replayed": response.headers.get("idempotent-replayed") }, duration: Math.round(performance.now() - start), time: new Date().toLocaleTimeString() });
  if (!response.ok) throw new Error(data.detail || data.title || `Request failed (${response.status})`);
  return { data, response };
}
const control = async (action) => {
  const result = await request(`/demo/worker/${action}`, { method: "POST", label: `Demo worker: ${action}` });
  server = result.data;
  renderWorker();
  return result.data;
};
function payload(recipients = state.recipients, title = state.course) {
  return { certificate: { title, issuer: state.issuer, issued_on: new Date().toISOString().slice(0, 10) }, recipients };
}
async function submit(body, key, label) {
  const result = await request(JOBS, { method: "POST", body, headers: { "Idempotency-Key": key }, label });
  state.jobId = result.data.id;
  job = result.data;
  items = [];
  save();
  return result;
}

function setStage(stage) {
  state.stage = stage;
  proof("");
  save();
  render();
  announce(steps[stage].title);
}
function renderWorker() {
  if (!server) return;
  $("worker-led").className = `worker-led ${server.worker_running ? "running" : state.stage === 6 ? "crashed" : ""}`;
  $("worker-label").textContent = server.worker_running ? "Worker online" : state.stage === 6 ? "Worker terminated" : "Worker offline";
  let detail = server.worker_running ? `Process ${server.worker_pid} · ${server.render_delay_ms} ms presentation delay` : "Queue persists on disk without a worker";
  if (state.stage === 6) {
    const lease = server.leases.find((item) => item.certificate_id === server.crashed_certificate);
    const seconds = lease ? Math.max(0, Math.ceil((lease.lease_expires_at - server.server_time_ms) / 1000)) : 0;
    detail = seconds > 0 ? `Claim expires in ${seconds}s · restart to recover` : "Lease expired · ready for a replacement worker";
  }
  $("worker-detail").textContent = detail;
}
function render() {
  const stage = state.stage;
  const step = steps[stage];
  $("step-number").textContent = `${String(stage + 1).padStart(2, "0")} / ${steps.length}`;
  $("guide-symbol").textContent = String(chapters[stage] + 1).padStart(2, "0");
  $("guide-title").textContent = step.title;
  $("guide-copy").textContent = step.copy;
  $("takeaway").textContent = step.notice;
  $("speaker-note").textContent = step.note;
  $("action-caption").textContent = step.caption;
  $("next").innerHTML = `${escape(busy ? "Working…" : step.action)} <span>${busy ? "…" : "→"}</span>`;
  $("next").disabled = busy || !server || ([3, 7, 8, 9].includes(stage) && !terminal(job)) || (stage === 1 && !state.dry?.would_create_job);
  $("restore").classList.toggle("hidden", stage !== 0);
  $("restore").disabled = busy;
  $("restart").disabled = busy;
  const activeJob = Boolean(job && !terminal(job));
  $("run-crash").disabled = busy || !server || activeJob;
  $("run-failure").disabled = busy || !server || activeJob;
  $("shortcut-hint").textContent = activeJob
    ? "Finish the current batch with the green walkthrough button before switching scenarios."
    : "Run either scenario without completing the earlier chapters.";
  renderSession();
  $("course").disabled = stage !== 0 || busy;
  $("issuer").disabled = stage !== 0 || busy;
  $("batch-settings").classList.toggle("hidden", stage >= 6);
  $("batch-kicker").textContent = stage >= 8 ? "FAILURE ISOLATION" : stage >= 6 ? "CRASH RECOVERY" : state.jobId ? "LIVE JOB" : "THE INPUT";
  $("batch-title").textContent = stage >= 8 ? "A failure you can fix." : stage >= 6 ? "A job that survives its worker." : state.jobId ? state.course : "A cohort, with a few imperfections.";
  document.querySelectorAll(".chapter").forEach((el) => {
    const n = Number(el.dataset.chapter);
    el.classList.toggle("active", n === chapters[stage]);
    el.classList.toggle("done", n < chapters[stage] && !(state.skippedChapters || []).includes(n));
    if (n === chapters[stage]) el.setAttribute("aria-current", "step"); else el.removeAttribute("aria-current");
  });
  document.querySelectorAll("[data-node]").forEach((el) => {
    el.classList.toggle("active", el.dataset.node === step.node);
    el.classList.toggle("inactive", el.dataset.node !== step.node);
  });
  $("flow-caption").textContent = stage === 0 ? "Start with the list. Follow the work all the way to disk." : step.notice;
  renderData();
  renderWorker();
  if (stage === 10 && state.directScenario) {
    $("guide-copy").textContent = "Your selected scenario is complete. Inspect the real files and API responses, try another feature above, or start a new walkthrough to explore the whole service.";
  }
}

function renderData() {
  const stage = state.stage;
  const dry = state.dry;
  let rows = state.recipients.map((row, row_index) => ({ ...row, row_index, status: "unvalidated", errors: [] }));
  if (job) rows = items;
  else if (dry) rows = rows.map((row) => {
    const errors = dry.errors.filter((e) => e.row_index === row.row_index);
    return { ...row, status: errors.length ? "rejected" : "valid", errors };
  });
  const counts = job?.counts;
  $("total-count").textContent = counts?.total ?? state.recipients.length;
  $("queue-count").textContent = counts ? counts.pending + counts.processing : dry?.counts.valid ?? "—";
  $("queue-label").textContent = counts ? "Still to process" : "Ready to generate";
  $("success-count").textContent = counts?.succeeded ?? 0;
  $("issue-count").textContent = counts ? counts.rejected + counts.failed : dry?.counts.rejected ?? "—";
  $("issue-label").textContent = counts?.failed ? "Rejected / failed" : "Need attention";
  const statusLabel = { queued: "Queued", processing: "Generating", completed: "Completed", completed_with_errors: "Done with exceptions", failed: "Generation failed" };
  $("job-badge").textContent = job ? statusLabel[job.status] : dry ? "Dry run complete" : "Not submitted";
  $("job-badge").className = `badge ${job ? terminal(job) ? job.status === "completed" ? "succeeded" : "pending" : job.status === "queued" ? "pending" : "processing" : "neutral"}`;
  $("progress-area").classList.toggle("hidden", !job);
  const percent = Math.round((job?.progress ?? 0) * 100);
  $("progress-value").textContent = `${percent}%`;
  $("progress-fill").style.width = `${percent}%`;
  document.querySelector("[role=progressbar]").setAttribute("aria-valuenow", String(percent));
  $("progress-label").textContent = terminal(job) ? "All valid recipients processed" : server?.worker_running ? "Generating in the background" : "Saved on disk · waiting for a worker";
  const labels = { unvalidated: "Not checked", valid: "Valid", rejected: "Rejected", pending: "Queued", processing: "Rendering", succeeded: "Ready", failed: "Failed" };
  $("recipients").innerHTML = rows.map((row) => {
    const editable = stage === 0 && !busy;
    const person = editable
      ? `<input class="recipient-input recipient-name" data-row="${row.row_index}" data-field="name" aria-label="Recipient ${row.row_index + 1} name" maxlength="120" value="${escape(row.name)}"><input class="recipient-input email" data-row="${row.row_index}" data-field="email" aria-label="Recipient ${row.row_index + 1} email" maxlength="254" value="${escape(row.email)}">`
      : `<span class="recipient-name">${escape(row.name)}</span><span class="recipient-email">${escape(row.email)}</span>`;
    const error = (row.errors || []).map((e) => friendlyError(e, row.status)).join(" · ");
    const recovered = state.stage === 7 && row.certificate_id === server?.crashed_certificate && row.status === "succeeded";
    return `<tr class="${row.status === "processing" ? "processing-row" : ""}"><td>${String(row.row_index + 1).padStart(2, "0")}</td><td>${person}</td><td><span class="badge ${row.status === "unvalidated" ? "neutral" : row.status}">${escape(labels[row.status])}</span>${row.status === "succeeded" ? `<button class="pdf-link" data-pdf="${escape(row.certificate_id)}" aria-label="Preview certificate for ${escape(row.name)}">PDF ↗</button>` : ""}${error ? `<p class="row-error">${escape(error)}</p>` : ""}${recovered ? '<span class="recovered">Recovered · same certificate ID</span>' : ""}${row.attempts ? `<span class="mobile-attempt">Attempt ${row.attempts}</span>` : ""}</td><td>${row.attempts ?? "—"}</td></tr>`;
  }).join("");
  $("table-note").textContent = job ? `Job ${job.id.slice(0, 12)}… · ${rows.length} rows · live updates every 650 ms` : dry ? "Dry run only. Nothing has been added to the queue." : "Illustrative recipients. Edit a name or email before validating.";
  const hasFiles = Boolean(counts?.succeeded);
  $("downloads").classList.toggle("hidden", !hasFiles);
  if (job) $("archive").href = `${JOBS}/${job.id}/archive`;
  $("archive").textContent = terminal(job) ? "Download ZIP ↓" : "Download current ZIP ↓";
}
function friendlyError(error, status) {
  if (error.code === "INVALID_EMAIL") return "Email address is not valid";
  if (error.code === "DUPLICATE_RECIPIENT") return `Duplicate email${error.duplicate_of_row_index !== undefined ? ` of row ${error.duplicate_of_row_index + 1}` : " in this batch"}`;
  if (error.code === "UNSUPPORTED_CHARACTERS") return "Template font cannot render these characters";
  if (error.code === "GENERATION_FAILED") return status === "failed" ? "Generation failed after retries" : "Render attempt failed · retry queued";
  return error.message || error.code;
}

async function refresh(log = false) {
  const id = state.jobId;
  const results = await Promise.all([
    request("/demo/state", { log: false }),
    ...(id ? [request(`${JOBS}/${id}`, { log, label: "Read job status" }), request(`${JOBS}/${id}/items?limit=1000`, { log, label: "Read recipient outcomes" })] : []),
  ]);
  // A previous poll must not overwrite a newly selected scenario.
  if (id !== state.jobId) return;
  server = results[0].data;
  if (id) { job = results[1].data; items = results[2].data.items; }
  $("connection").classList.add("hidden");
  renderWorker();
  if (state.stage > 0) render();
  else $("next").disabled = busy;
}
async function poll() {
  try {
    if (!busy) {
      const wasTerminal = terminal(job);
      await refresh(firstPoll && Boolean(state.jobId));
      firstPoll = false;
      if (!wasTerminal && terminal(job)) {
        announce(`Batch finished. ${job.counts.succeeded} certificates ready, ${job.counts.failed} failed, ${job.counts.rejected} rejected.`);
        await request(`${JOBS}/${job.id}/items?limit=1000`, { label: "Final recipient outcomes" });
        if (state.stage === 7) {
          const recovered = items.find((row) => row.certificate_id === server.crashed_certificate);
          if (recovered?.status === "succeeded" && recovered.attempts > 1) proof(`Recovered: the original certificate ID succeeded on attempt ${recovered.attempts}. ${job.counts.succeeded}/${job.counts.total} recipients finished.`);
        }
        if (state.stage === 9 && state.retryIds) {
          const stable = items.every((row) => state.retryIds[row.row_index] === row.certificate_id);
          if (stable && job.counts.failed === 0) proof("All recipients succeeded. Certificate IDs match the pre-retry snapshot, including Grace’s.");
        }
      }
    }
  } catch (error) {
    $("connection").textContent = `Live updates interrupted: ${error.message}. Check the demo server; updates will reconnect automatically.`;
    $("connection").classList.remove("hidden");
  } finally { pollTimer = setTimeout(poll, 650); }
}

async function runCrashScenario() {
  await control("stop");
  const recipients = ["Ada Lovelace", "Alan Turing", "Katherine Johnson", "Margaret Hamilton", "Donald Knuth", "Barbara Liskov"].map((name, i) => ({ name, email: `recovery${i + 1}@example.org` }));
  state.crashPayload ??= payload(recipients, "Building Resilient Systems");
  save();
  const result = await submit(state.crashPayload, state.crashKey || `${state.key}-crash`, "Queue the crash recovery batch");
  state.crashJobId = result.data.id;
  await control("crash");
  setStage(6);
  proof("Worker killed after a committed claim. The API still responds; no recovery rows were edited by the demo.");
}
async function runFailureScenario() {
  await control("stop");
  state.failurePayload ??= payload([
    { name: "Grace Hopper", email: "grace@example.org" },
    { name: "James Gosling", email: "james@example.org" },
    { name: "Radia Perlman", email: "radia@example.org" },
    { name: "Tim Berners-Lee", email: "tim@example.org" },
  ], "Reliable Software Engineering");
  save();
  const result = await submit(state.failurePayload, state.failureKey || `${state.key}-failure`, "Queue the failure isolation batch");
  state.failureJobId = result.data.id;
  await control("failure");
  setStage(8);
}
async function directScenario(kind) {
  if (busy || !server || (job && !terminal(job))) return;
  busy = true;
  render();
  proof("");
  try {
    state.directScenario = true;
    state.skippedChapters = Array.from({ length: kind === "crash" ? 3 : 4 }, (_, i) => i);
    // Direct runs get fresh jobs. Fault scenarios use their own valid certificate
    // metadata even if the editable starter batch has an invalid title or issuer.
    const recipients = kind === "crash"
      ? ["Ada Lovelace", "Alan Turing", "Katherine Johnson", "Margaret Hamilton", "Donald Knuth", "Barbara Liskov"].map((name, i) => ({ name, email: `recovery${i + 1}@example.org` }))
      : [{ name: "Grace Hopper", email: "grace@example.org" }, { name: "James Gosling", email: "james@example.org" }, { name: "Radia Perlman", email: "radia@example.org" }, { name: "Tim Berners-Lee", email: "tim@example.org" }];
    state[`${kind}Payload`] = {
      certificate: { title: kind === "crash" ? "Building Resilient Systems" : "Reliable Software Engineering", issuer: "Acme Academy", issued_on: new Date().toISOString().slice(0, 10) },
      recipients,
    };
    state[`${kind}Key`] = `${state.key}-${kind}-${crypto.randomUUID()}`;
    if (kind === "crash") {
      // Its subsequent failure chapter must also be a fresh run.
      state.failurePayload = null;
      state.failureKey = `${state.key}-failure-${crypto.randomUUID()}`;
      await runCrashScenario();
    } else await runFailureScenario();
    await refresh(true);
  } catch (error) { proof(error.message, true); }
  finally { busy = false; render(); save(); }
}

async function advance() {
  if (busy) return;
  if (state.stage === 10) { $("restart-dialog").showModal(); return; }
  busy = true;
  render();
  proof("");
  try {
    switch (state.stage) {
      case 0: {
        state.payload = payload();
        const result = await request(`${JOBS}?dry_run=true`, { method: "POST", body: state.payload, label: "Validate without storing" });
        state.dry = result.data;
        setStage(1);
        proof(result.data.would_create_job ? `${result.data.counts.valid} valid · ${result.data.counts.rejected} rejected · 0 jobs created by dry run.` : "Every row was rejected. Start a new walkthrough to edit the sample and try again.");
        break;
      }
      case 1: {
        const result = await submit(state.payload, state.key, "Submit the cohort");
        state.originalJobId = result.data.id;
        setStage(2);
        proof(`HTTP ${result.response.status} · Job saved. ${result.data.counts.pending} certificates queued.`);
        break;
      }
      case 2: await control("start"); setStage(3); break;
      case 3: setStage(4); break;
      case 4: {
        const result = await request(JOBS, { method: "POST", body: state.payload, headers: { "Idempotency-Key": state.key }, label: "Replay the identical request" });
        if (result.data.id !== state.originalJobId || result.response.headers.get("idempotent-replayed") !== "true") throw new Error("The replay did not match the original job.");
        setStage(5);
        proof(`HTTP ${result.response.status} · Idempotent-Replayed: true · Same job ID: ${result.data.id.slice(0, 12)}…`);
        break;
      }
      case 5: await runCrashScenario(); break;
      case 6: await control("start"); setStage(7); break;
      case 7: await runFailureScenario(); break;
      case 8: {
        state.retryIds = Object.fromEntries(items.map((row) => [row.row_index, row.certificate_id]));
        save();
        await control("start");
        await request(`${JOBS}/${state.jobId}/retry`, { method: "POST", label: "Retry failed certificates only" });
        setStage(9);
        break;
      }
      case 9: setStage(10); proof("Walkthrough complete. Open a certificate or the API inspector to explore the evidence."); break;
    }
    await refresh(true);
    save();
  } catch (error) {
    proof(error.message, true);
    if (state.stage === 0) { state.dry = null; save(); }
  } finally {
    busy = false;
    render();
  }
}

async function preview(certificateId) {
  const url = `/api/v1/certificates/${encodeURIComponent(certificateId)}`;
  try {
    // The production download uses Content-Disposition: attachment. A Blob URL
    // embeds those exact bytes without changing the API's download contract.
    const response = await fetch(url, { signal: AbortSignal.timeout(12000) });
    if (!response.ok) throw new Error(`PDF download failed (${response.status})`);
    const blob = await response.blob();
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(blob);
    $("pdf-frame").src = `${previewUrl}#view=FitH`;
    $("pdf-download").href = url;
    if (!$("pdf-dialog").open) $("pdf-dialog").showModal();
  } catch (error) { proof(error.message, true); }
}
$("next").addEventListener("click", advance);
$("run-crash").addEventListener("click", () => directScenario("crash"));
$("run-failure").addEventListener("click", () => directScenario("failure"));
$("reload-demo").addEventListener("click", () => { if (!busy) { save(); if (storageAvailable) location.reload(); } });
$("recipients").addEventListener("input", (event) => {
  const { row, field } = event.target.dataset;
  if (row !== undefined && state.stage === 0) { state.recipients[Number(row)][field] = event.target.value; save(); }
});
$("recipients").addEventListener("click", (event) => {
  const button = event.target.closest("[data-pdf]");
  if (button) preview(button.dataset.pdf);
});
$("course").addEventListener("input", (event) => { state.course = event.target.value; save(); });
$("issuer").addEventListener("input", (event) => { state.issuer = event.target.value; save(); });
$("restore").addEventListener("click", () => { state.recipients = sample(); save(); render(); });
$("preview").addEventListener("click", () => { const row = items.find((item) => item.status === "succeeded"); if (row) preview(row.certificate_id); });
$("close-preview").addEventListener("click", () => $("pdf-dialog").close());
$("pdf-dialog").addEventListener("close", () => {
  $("pdf-frame").src = "about:blank";
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
});
$("call-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-call]");
  if (button) { selectedCall = Number(button.dataset.call); renderCalls(); }
});
$("present").addEventListener("click", async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch { proof("Fullscreen is unavailable in this browser. You can still present in the normal window."); }
});
$("restart").addEventListener("click", () => $("restart-dialog").showModal());
$("cancel-restart").addEventListener("click", () => $("restart-dialog").close());
$("confirm-restart").addEventListener("click", async () => {
  if (busy) return;
  busy = true;
  $("confirm-restart").disabled = true;
  try {
    // Do not stop an active generation and strand its queued items.
    if (job && !terminal(job)) {
      throw new Error("Finish this batch before restarting the walkthrough. Continue with the next walkthrough action.");
    }
    await control("stop");
    state = fresh(); job = null; items = []; calls = [];
    restored = false;
    $("course").value = state.course; $("issuer").value = state.issuer;
    save(); proof(""); renderCalls();
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (error) { proof(error.message, true); }
  finally { busy = false; $("confirm-restart").disabled = false; $("restart-dialog").close(); render(); }
});

async function boot() {
  $("next").disabled = true;
  try {
    const result = await request("/demo/state", { log: false });
    server = result.data;
    storageKey = `certgen-live-demo:${server.session}`;
    try {
      const saved = sessionStorage.getItem(storageKey);
      state = JSON.parse(saved) || fresh();
      restored = Boolean(saved);
    } catch { state = fresh(); storageAvailable = false; }
    if (!Number.isInteger(state.stage) || !steps[state.stage] || !Array.isArray(state.recipients)) state = fresh();
    if (restored) {
      $("restored-state").textContent = `Restored step ${state.stage + 1} of ${steps.length}${state.jobId ? ` · same job ${state.jobId.slice(0, 12)}…` : " · your saved batch"}.`;
      document.querySelector(".session-panel").open = true;
    }
    $("course").value = state.course; $("issuer").value = state.issuer;
    await refresh(Boolean(state.jobId));
    render();
    poll();
  } catch (error) {
    $("connection").textContent = `Cannot reach the demo server: ${error.message}. Start scripts/demo-live.sh and reload this page.`;
    $("connection").classList.remove("hidden");
  }
}
window.addEventListener("pagehide", () => clearTimeout(pollTimer));
boot();
