# Presenting certgen

## Start in one command

From the project directory, after the README setup:

```bash
./scripts/demo-live.sh
```

The page opens at **http://127.0.0.1:8010**. On Windows, use
`.venv\Scripts\python scripts\live_demo.py`. No frontend build, npm installation, account, or internet
connection is required. The normal production service does not need to be running.

For the cleanest presentation, use a desktop browser and a wide window. **Present** requests browser
fullscreen. Keep **Presenter notes** open if you want prompts; expand the API inspector only when
explaining implementation details. On smaller screens the guide moves below the recipient table.

The green button always advances the story. During generation it waits for a terminal job state.
You can take as much time as you need between steps, including between the crash and recovery.

For a quick demonstration, use **Run crash demo** or **Run failure demo** in the **Try a feature
directly** bar above the recipient table. These create their own sample batches and jump straight to
the relevant chapter. After a crash, use **Start a replacement worker**; after the individual failure
finishes, use **Remove the fault & retry**. Shortcuts are disabled while the current batch is unfinished.

Expand **Isolated demo session** to see this launch's session ID and data directory. Click **Reload
this tab** to verify that the current step and job are restored; the panel reports the restored step
after reloading. This is per-server-launch data isolation, not a separate database for each browser tab.

[Desktop preview](evidence/live-demo-desktop.png) ·
[Generated PDF preview](evidence/live-demo-certificate.png) ·
[Verification results](evidence/live-demo-checks.txt)

## A five-minute walkthrough

| Chapter | Do this | Say this | Point at |
| --- | --- | --- | --- |
| 1. Check the batch | Show the editable sample, then **Validate this batch**. | “Imagine issuing certificates to an entire cohort. A few mistakes in the list should not stop everyone else.” | Twelve sample recipients; nine valid rows and three rejected rows. The problems are an invalid email, an unsupported name, and a duplicate email. |
| 1. Check the batch | **Submit this batch**. | “The dry run did not store anything. Now one request saves the batch and returns immediately.” | HTTP 202 in the API inspector; nine queued items; zero PDFs. |
| 2. Watch it work | **Start generation**. | “A worker claims one recipient at a time. The web request is already finished, and we can keep checking progress.” | Queued → rendering → ready, live counts, attempts, and the worker process ID. |
| 3. See the output | **Explore the output**, then **Preview certificate**. Close the preview and **Download ZIP**. | “These are the actual generated files. The manifest explains every row, including the ones we rejected.” | Recipient, course, issuer, date, certificate ID; PDFs and `manifest.csv` inside the ZIP. |
| 3. See the output | **Replay the same request**. | “If a client loses its response, it can safely resend this request with the same key.” | HTTP 200, `Idempotent-Replayed: true`, the original job ID. |
| 4. Survive a crash | **Demonstrate a worker crash**. Pause. | “We killed a real worker during a render. The work is still recorded in SQLite.” | Worker terminated; one claimed item still processing; remaining recipients queued. |
| 4. Survive a crash | **Start a replacement worker**. | “The replacement worker continues the batch. After the old claim expires, it reclaims that item too.” | Six completed PDFs; the recovered row has attempt 2 and the same certificate ID. |
| 5. Fix & retry | **Show an individual failure**. | “Now only Grace's render raises an intentional error. Everyone else still gets a certificate.” | Three successes, one failure; Grace reaches the three-attempt limit. |
| 5. Fix & retry | **Remove the fault & retry**. | “After fixing the cause, we retry only failed work. The existing successful certificates stay as they are.” | Four successes; Grace's certificate ID is retained and her attempt counter resets. |
| Finish | **Finish the walkthrough**. | “The important part isn't just generating PDFs. It's knowing what happened to every recipient and recovering without losing work.” | The architecture diagram and request/response history. |

The sample data is illustrative. Before validating, you can edit names, emails, the course title, and
the issuer. Dates use today's UTC date automatically. Editing the sample may change the expected
counts; **Restore sample** restores the names and emails. If you make every row invalid, start a new
walkthrough and correct the sample.

## What is real, and what is controlled

- All submission, dry-run, replay, status, retry, PDF and ZIP requests go to the existing production
  FastAPI application, mounted unchanged under `/api`. Its interactive documentation is at `/api/docs`.
- Workers use the real SQLite claims, leases, fencing, attempt limits, ReportLab renderer and atomic
  file storage. The browser polls for statuses; it never invents counts or completion events.
- The demo initially holds its worker offline to expose the queue boundary. The normal server starts
  embedded workers by default.
- A 650 ms rendering delay makes state changes visible. This is not a performance measurement.
- Leases are shortened from the production default of 30 seconds to 8 seconds. If you pause after
  the crash, the lease can expire before you start the replacement. Recovery still works.
- The crash controller starts a worker with a paused renderer, waits for its committed claim, then
  calls `Popen.kill()` on that child only. On Linux/macOS this is SIGKILL; on Windows it terminates the
  process. The controller does not manipulate database statuses.
- The failure worker raises an `OSError` for `grace@example.org` only. This models a generation
  failure; it does not fill the disk or change filesystem permissions. The fix starts a normal worker
  and calls the actual retry endpoint.
- Crash recovery demonstrates another attempt with the same certificate ID. Token fencing is part
  of the real worker; the existing tests separately cover a stale worker returning after expiry.
- The demo has no QR verification or email delivery. PDFs use the project's existing template.

## Files, sessions and cleanup

Each launcher invocation creates `data/live-demo/<timestamp>-<random>/` containing:

- `certgen.sqlite3`: the demo jobs and queue.
- `certificates/<job-id>/<certificate-id>.pdf`: the actual outputs.
- `worker.log`: logs from this demo's worker processes.
- `claimed`: the certificate ID involved in the controlled crash, when that chapter runs.

Reloading the page in the same tab resumes its walkthrough step. **Start a new walkthrough** clears
the presentation state and uses new idempotency keys; it preserves previous jobs and files. Complete
an active batch before restarting so queued work is not stranded. Use a single tab when presenting:
the worker belongs to the local demo session, so multiple tabs would control the same worker.

**Ctrl+C** stops the launcher and its worker. Files are retained; an old session directory can be
deleted when you no longer need its artifacts. Each new launch starts a fresh, isolated session.
The application never deletes or reuses normal `data/certgen.sqlite3` application data.

## Troubleshooting and verification

| Symptom | Action |
| --- | --- |
| Port 8010 is occupied | Run `./scripts/demo-live.sh --port 8011` and use the printed URL. |
| Browser does not open | Open the printed URL manually; `--no-browser` disables automatic opening. |
| Missing Python dependencies | Run `.venv/bin/python -m pip install -r requirements.txt`. |
| A moved project breaks a `.venv/bin/...` tool | The launcher uses `.venv/bin/python` directly. For tools, use `.venv/bin/python -m pytest` / `-m mypy`, or recreate the virtual environment if its Python symlink is also broken. |
| “Live updates interrupted” | Check that the terminal server is still running. Updates reconnect automatically. After restarting the server, reload the page to load its new session. |
| PDF is downloaded rather than displayed by the browser | Use **Open / save PDF** in the preview. PDF support varies by browser; the downloaded file contains the same bytes. |
| A worker fails to start or a job stops progressing | Read `worker.log` inside the session path printed by the launcher. A new launcher invocation provides a clean session. |

Run the repeatable integration check:

```bash
.venv/bin/python scripts/check_live_demo.py
```

It starts its own server on a temporary localhost port and checks dry-run non-persistence, partial
acceptance, PDF bytes, ZIP and manifest contents, idempotent replay, real process termination, lease
recovery, absence of duplicate PDFs, individual failure isolation, and retry with stable IDs. It stops
its server/workers and removes its temporary data. It requires permission to bind a localhost socket.

The original terminal presentation remains available as `scripts/demo.sh`. The browser walkthrough
above is the recommended presentation for an audience unfamiliar with the project.
