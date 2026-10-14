<img src="docs/banner.svg" alt="Jev Ultrafast · Browser Use" width="100%" />

# Jev Ultrafast for Nimi

**A browser agent with a dynamic, indexed action space**, adapted to run as a Nimi desktop App: an
Electron Host plus this App-owned Python worker. Upstream: [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) (MIT),
adapted from commit `1231850`.

Give it one goal. A decision model picks an operation, then its element. A text model writes text only
when the operation is `TYPE_TEXT`. Nimi decides which model answers, for example TypeSafe Jev in the cloud
or a local checkpoint; the App neither knows nor branches on it.

## The action space

Every observation produces a new element table:

```text
[1] button    Change ticket type · Round trip
[2] combobox  Where from?        · San Francisco
[3] combobox  Where to?          · empty
[4] textbox   Departure          · empty
...
```

The operations are `CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL_UP`, `SCROLL_DOWN`, `WAIT`, `DONE`, and
`BLOCKED`. Only supported operations and targets are offered.

```text
page → element table → text.decide: which operation?
                          │
                          ├─ WAIT · DONE · BLOCKED · SCROLL_* ─────────────→ browser   (1 call)
                          │
                          └─ CLICK · TYPE_TEXT · SELECT
                               ↓ same observation, rest of the same budget
                             text.decide: which element for that operation?          (2nd call)
                               ↓ skipped when only one element qualifies
                             CLICK [7] / SELECT [5:2] ──────────────────────→ browser
                             TYPE_TEXT [3] → text.generate {"text": …} ──────→ browser
```

A decision is **one or two `text.decide` calls** on one observation. The target question uses the same
candidate descriptions and instructions as upstream, and each target question contains only compatible
elements. A single qualifying element is taken without a model question and carries no probability. Before
the second call and before any browser input the worker checks that the run is still active. Each decision
records `calls`, `operation_latency_ms`, `target_latency_ms` and `latency_ms`; each executed step records
`decision_calls`.

There are no site-specific action scripts or prepared field strings in the policy. Model output never
becomes selectors, coordinates, shell commands or executable JavaScript. Probabilities are validated
(submitted ids exactly, finite, summing to 1, the selected one is the most probable) and never invented.

## How it runs in Nimi

- **Worker**: `python -m jev_ultrafast.worker` (or the `jev-worker` script) speaks newline-delimited JSON on
  stdin/stdout in strict UTF-8. stderr carries logs only.
- **AI through the Host**: the worker sends bounded `ai_request` messages; the Host runs `text.decide` or
  `text.generate` (structured output) with Nimi and answers with `ai_result`. Python holds no credentials,
  runs no model, never sees Runtime endpoints and cannot ask for any other Runtime method.
- **Dedicated Chrome**: the worker launches its own Chrome with its own `--user-data-dir` and
  `--remote-debugging-port=0`, reads `DevToolsActivePort`, and points Browser Harness at it with
  `BU_CDP_URL` and a unique `BU_NAME`. It never attaches to your Chrome, refuses personal profile
  directories, and needs no "Allow remote debugging" prompt. On Windows, Chrome and the harness daemon run
  in one kill-on-close job: `close`, or the worker exiting for any reason, ends exactly that tree.
- **Fixtures**: the travel and research presets load `static/fixture.html` from a loopback-only server the
  worker owns (random port, that one page, no API).
- **Inspector**: `static/index.html`, `style.css` and `app.js` keep the upstream look. `app.js` talks only to
  an injected `window.jevTransport` that the Host provides.
- **Electron Host** (`src-electron/`): binds the Nimi Host profile first, then registers Kit's App bridge with
  one fixed App command, `jev.worker.command` (`{name, body}`: only the worker's command names, a JSON body of
  at most 64 KiB), and forwards the worker's state events to the window as `jev.worker.state`. `decide`
  requests run with the SDK client's `ai.scenario.execute` (the spec already is a `text-decide` spec);
  `generate_text` becomes a `text-generate` turn (a system message, the user messages, a strict json-schema
  response format, `maxTokens`). The worker receives `{type, answers, traceId}` or the completed text; a
  failure is answered with `{code: reasonCode ?? code ?? name, message}`, with Kit's kebab-case AI reason codes
  restored to the Runtime spelling (for example `AI_INPUT_LIMIT_EXCEEDED`). Stop and a protected-session change
  abort every in-flight AI request.
- **Worker process**: development runs the project `.venv` Python with `-m jev_ultrafast.worker` from the App
  root; the packaged App runs the PyInstaller build at `resources/jev-worker/jev-worker.exe`. The worker gets
  an explicit environment (no Nimi Host variables, credentials or proxies) and a per-run directory under the
  Host profile's temp directory for its automation profile, runtime files and `TEMP`; the Host removes it after
  the worker exits and removes directories an earlier Host left behind. Worker output is framed strictly:
  invalid UTF-8, a line over 4 MiB or a message outside the protocol stops the worker. After that or a crash,
  only Start launches a new worker, and nothing is replayed. Quitting sends `close` and ends the worker's
  process tree if it does not exit.
- **Renderer** (`renderer/`): Vite serves and builds `static/index.html` with `app.js` and `style.css`
  unchanged, behind a small entry that first installs `window.jevTransport` over Kit's renderer bridge
  (`invoke`, `listenShell`). The desktop App loads no web font; the stylesheet's system UI fallback applies.

| Worker environment | Meaning |
| --- | --- |
| `JEV_CHROME_PATH` | Chrome executable. Default: the standard Google Chrome install location. The Nimi Host passes it through when it is set. |
| `JEV_BROWSER_PROFILE_DIR` | Automation profile directory, kept by the worker. The Nimi Host sets a per-run directory and removes it after the worker exits. Default: a temporary profile removed on close. |
| `JEV_RUNTIME_DIR` | Parent for the worker's temporary runtime files. The Nimi Host uses the same per-run directory. Default: the system temp directory. |
| `JEV_CHROME_HEADLESS=1` | Run the automation Chrome headless. The Nimi Host passes it through. |

## Worker protocol

One JSON object per line, at most 4 MiB. Unknown message types or fields, duplicate keys, `NaN`, invalid
UTF-8 and unpaired surrogate escapes are rejected; a rejected command with a valid id gets an
`invalid_command` error, anything else is logged to stderr and ignored.

```text
Host → worker
{"type":"command","id":N,"name":NAME,"body":{…}}
{"type":"ai_result","id":M,"ok":true,"result":{…}}
{"type":"ai_result","id":M,"ok":false,"error":{"code":"…","message":"…"}}

Worker → Host
{"type":"response","id":N,"ok":true,"state":{…}}
{"type":"response","id":N,"ok":false,"error":{"code":"…","message":"…"}}
{"type":"ai_request","id":M,"kind":"decide"|"generate_text","spec":{…},"timeoutMs":T}
{"type":"event","name":"state","state":{…}}
```

| Command | Body | Effect |
| --- | --- | --- |
| `start` | `{"url","goal"}` | Open an http(s) start URL (a navigation target only) with a 1–2,000 character goal. |
| `preset` | `{"scenario","goal"?}` | `travel` or `research` (local fixture) or `flights` (Google Flights, a date 30 days ahead, stops at visible results, never books). |
| `predict` | `{}` | Observe if needed, then decide (one or two `text.decide` calls). |
| `act` | `{"fingerprint"}` | Execute the current decision on the page it was made for. |
| `tick` | `{}` | `predict` then `act`; a stale page is observed again without acting. |
| `auto` | `{"pace"?}` | Repeat until DONE, BLOCKED, a stop, an error or 120 decisions (at most 60 executed actions); a state event after each step. |
| `stop` | `{}` | Answered at once, even while another command waits for a model. |
| `state` | `{}` | Answered at once. While busy, `state.busy` names the running command. |
| `close` | `{}` | Stop, close the tab, daemon, Chrome and fixture server, reply, exit. |

Commands run one at a time; another command meanwhile gets `busy`. **Stop** invalidates the running
decision or auto loop: a pending wait ends immediately, an `ai_result` that arrives later is discarded and
never executed, browser input that already started is not rolled back, and the next `predict`/`tick`/`auto`
observes the page again before deciding.

`decide` specs mirror the Nimi SDK `TextDecideScenarioSpec` with exactly one choice question:

```json
{"type":"text-decide","state":{"json":{"page":{…},"elements":[…],"recent_actions":[…]}},
 "questions":[{"id":"operation","kind":"choice","instructions":{"json":{"goal":"…","rules":"…"}},
   "candidates":[{"id":"CLICK","description":{"text":"…"}},{"id":"WAIT","description":{"text":"…"}}]}]}
```

The result is `{"type":"text-decide","answers":[{"questionId","kind":"choice","selectedCandidateId",
"probabilities":[{"candidateId","probability"}]}],"traceId"?}`. `generate_text` specs are
`{"type":"text-generate","systemPrompt","input":[{"role":"user","content"}],"responseFormat":{"kind":
"json_schema","schemaName","strict":true,"jsonSchema"},"maxTokens"}` for `{"text": string|null}`
(at most 2,000 characters); the result is `{"type":"text-generate","text":"<model output>","traceId"?}`,
and anything but one non-empty string types nothing. A decision shares a 60-second budget; the target
question gets what remains of it.

Command error codes: `invalid_command`, `busy`, `stopped`, `canceled`, `stale_page`, `ai_error`,
`ai_timeout`, `ai_request_rejected`, `decision_contract`, `browser_unavailable`, `browser_error`,
`rejected`, `internal_error`. Host error codes are handled generically and all fail closed:
`OPERATION_ABORTED` is a cancel (discarded, never executed), `OPERATION_TIMEOUT` a timeout,
`AI_INPUT_INVALID`/`SDK_LOCAL_APP_INPUT_INVALID` a rejected request, `AI_INPUT_LIMIT_EXCEEDED` a blocked
step whose reason is shown (the command itself succeeds), and every other code an `ai_error` naming it.

## Development

```bash
python -m venv .venv && .venv/Scripts/python -m pip install uv   # or use an installed uv
uv sync
uv run ruff check .
uv run pytest
node --test tests/renderer.test.mjs
node --check jev_ultrafast/static/app.js
node --check jev_ultrafast/snapshot.js
uv build
```

Tests are offline: no model, no network and no Chrome (a few start Python subprocesses, including the
worker itself to check its stream encoding). `static/dev-transport.mjs` is a fake `window.jevTransport` for renderer tests; it is not in the
wheel. The Node tests also cover the real transport under `app.js` and the Host's worker protocol against a
scripted fake worker (`tests/fixtures/fake-worker.mjs`).

### Nimi desktop App

Requires Node 24 with pnpm 10, the project `.venv` above, Google Chrome, and a Nimi Desktop that supervises
App-owned renderer commands. SDK 0.21.0, Kit/native 0.21.0, App Tools 0.13.1 and Nimi Coding 0.6.5 now resolve
from the public registries. The lockfile pins the selected packages, and Electron 44 provides the Host.
No Nimi source checkout or local package archives are required. App release CI and real browser-agent
acceptance are separate checks; a dependency install does not establish either result.

```bash
pnpm install                     # also installs the Electron binary (install-electron --no)
pnpm setup:worker                # pinned Python 3.12.13, locked worker/test/build environment
pnpm dev                         # Nimi Desktop supervises the Host; renderer: dev:renderer on http://127.0.0.1:1533
pnpm test                        # nimi-app test: ruff, pytest, Node tests, syntax checks
pnpm typecheck
pnpm build:electron              # dist-electron/main.js + preload.cjs
pnpm check                       # nimi-app check
pnpm build:electron:production   # renderer, Host, PyInstaller worker -> dist-electron-package/jev-ultrafast-shell-win32-x64
pnpm test:packaged-worker        # packaging/connection check of the packaged worker (scripted AI answers)
```

`build:worker` installs PyInstaller 6.22.3 into the project `.venv` if needed (build tooling, not an App
dependency). Work files stay under `.nimi/local/`.

Two scripts use a real, dedicated Chrome and no model:

- `uv run python scripts/check_guards.py` runs the freshness, occlusion and execution guards on real controls.
- `uv run python scripts/worker_smoke.py` is a **connection smoke**: it drives the real worker over its
  protocol, answers every decision with a fixed `WAIT` (not a model result), and checks launch, attach,
  observation, stop, cleanup and that your own Chrome is untouched (Windows).

The upstream live examples, measurement and recording scripts and the HTTP inspector server needed model
keys inside Python and are not part of this adaptation; they remain in the upstream history.

## Evidence and limits

### Windows delivery (2026-10-15)

The earlier uncommitted dependency/session work is preserved in `007bb31`.
Current 0.1.3 uses the published package combination above and
retains the existing decision loop. Tests and production builds explicitly
prepare the project-local Python environment with pinned uv; PyInstaller and
its hooks are in the frozen uv lock rather than installed through an assumed
venv pip. The product itself never installs its worker dependencies on launch.
No model prompt, decision threshold, site-specific rule or DONE verification
was changed. Old 0.1.2 local-import data and packages remain separate.

[0.1.3](https://github.com/nimiplatform/jev-ultrafast/releases/tag/v0.1.3) is now
published through the protected tag workflow after a successful full branch
build. Registry preparation verified the exact license, digest and build
provenance; [PR 110](https://github.com/nimiplatform/nimi-app-registry/pull/110)
passed candidate checks and awaits its explicit human admission decision for
Windows x86_64 / stable. No Catalog install or installed business result is
inferred from that publication.

The current combination passes 112 Python tests, 27 Node tests, syntax/type
checks, frozen installation, production build and production pack. The frozen
worker check starts its own real Chrome, verifies Unicode text transport and
Stop/cleanup, and preserves user Chrome; its model answers are scripted, so it
is packaging/protocol evidence only.

On the locally installed Nimi 0.2.5-rc.2 fixed service, a separate supervised
development registration completed a real custom task: start at example.com,
open its information link, and stop at the IANA Example Domains page. Two
actual Nimi decision calls selected CLICK (207 ms) and DONE (235 ms); each was
executed through the product controls. The actual destination URL and page
were independently observed. The App correctly retained `verification.passed`
as null for a custom goal; the model's DONE was not promoted to an automatic
verification. Product export saved the 29,115-byte trace at
`.nimi/local/acceptance-0.1.3/example-domain-trace.json`. Normal App close removed
the dedicated Chrome processes. These call timings are not a speed/reliability
claim. No text-helper call occurred, so live text generation with this package
combination remains NOT-VERIFIED; the previously deferred decision-quality
issues are unchanged. Installed-package/Catalog acceptance and other platforms
remain separate. Nimi 0.2.5-rc.2 is a local candidate, not a public Nimi release.

The publisher declaration is general-audience and includes unmoderated web
content. The agent can type model-generated text and click a submit control on
a task website without a separate confirmation for each action, so its text
exposure is declared publishable with automatic publication control. This adds
no permission or new sending behavior. Website accounts may be needed for some
tasks; the App still uses only its dedicated temporary Chrome profile, never
the user's existing browser or its saved credentials.

### Deferred decision-quality issue (2026-10-11)

The current local Laya model selected DONE before typing `Nimi` into Wikipedia's search field. The page did not satisfy the goal. This remains a model/App decision-quality issue to investigate separately; the user explicitly deferred it while VoiceStudio cloud speech and OpenWhispr test migration are repaired. No prompt tuning, model replacement, site-specific rules or completion bypass is included in that repair scope.

On 2026-10-12 the user reaffirmed that this decision-quality issue remains deferred while MiMo Runtime tool-text support is repaired; no model-quality code was changed.

### Verification history

- **Technical-session recovery:** explicit new worker commands prepare the Kit session before dispatch; Stop, shutdown and invalidation abort pending preparation. No failed command is replayed. The 18 Host protocol tests and typecheck/build/check passed. After a real Runtime restart, a new explicit Choose next obtained another real decision in the same supervised Host.
- **Published packages (2026-10-11, Windows development):** SDK 0.21.0, Kit/native 0.18.1 and App Tools 0.11.4 resumed the existing Desktop-supervised registration. A real local decision selected CLICK on example.com's only link, then DONE after reaching `https://www.iana.org/help/example-domains`; the actual destination page was visually verified. Navigation interrupted the first post-click observation, so an explicit subsequent Choose next observed the destination without replaying the click. The history now labels that unobserved immediate result honestly instead of claiming no change. The five renderer checks and syntax check passed; prior package-upgrade tests/type checks/Host build remain applicable. A second real goal, typing `Nimi` into Wikipedia's search field without submitting, did not succeed: the model selected DONE before any action, so the text helper was not exercised. This is a recorded model decision failure, not a passing task. New-package text-generation execution, installed operation and broader sites remain **NOT-VERIFIED**.
- **Nimi runs with a local decision model (2026-09-24, Windows 11, RTX 5090)**: Decisions = Laya browser
  steps (v10s), Text generation = Gemma 4 E2B, both on this device, in the Desktop-supervised development App
  and the locally imported installed App. Each step asks one or two `text.decide` questions (about 0.6-1.2 s
  each once the model is resident; the first call after a Runtime restart waited 44 s for the model to load).
  Typed text comes from `text.generate`, including Chinese text, and reads back unchanged from the page.
  Stop discards a pending decision and nothing more runs. With this local model the tasks did not finish:
  the Travel fixture ended in a DONE that failed the independent check, the Wikipedia search typed the query
  and then kept clicking the search box, Google Flights picked unrelated controls, and Chinese goals were
  weak. Runs with Jev (TypeSafe) through Nimi: NOT-VERIFIED, no account yet.
- **Local, model-free checks**: 21 browser guard checks pass in a dedicated Chrome, and the connection smoke
  passes on Windows 11 with Chrome.
- **Packaged worker, a packaging/connection check with scripted AI answers (not a model result)**:
  `pnpm test:packaged-worker` passes on Windows 11 with Chrome. Driven through the Host's worker module, the
  packaged `jev-worker.exe` serves its fixture page, starts its own Chrome and daemon, returns a Chinese +
  emoji goal unchanged, types a fixed Chinese text that is read back from the page, discards a stopped
  decision, and leaves no automation Chrome, daemon or run directory after close. The same check passes
  against the development `.venv` worker (`node scripts/check-packaged-worker.mjs --source`).
- **Upstream history, not a Nimi metric**: the upstream TypeSafe-direct build recorded a 7.073 s Google
  Flights run with one combined request per decision. See [performance.md](docs/performance.md).

Page content is untrusted: text and links on a visited page can influence which control the agent chooses
and what it types, including on another site it navigates to. Do not put passwords or personal details in a
goal that you would not enter on an arbitrary website. Downloads go into the run's temporary automation
profile and are removed with it; they never land in your own Downloads folder.

A `DONE` choice still requires independent outcome verification. The unedited presets run an independent
check on a fresh read of the final page; a custom or edited goal reports DONE as the model's claim only.
The DOM reader handles common HTML and ARIA controls, not the full accessible-name specification. Shadow
roots, frames, canvas, uploads, pop-up tabs, nested scrolling and arbitrary keyboard widgets remain outside
this MVP. A page too large for one `text.decide` request (256 KiB of state, 8 KiB per candidate) blocks
the run with that reason rather than being truncated.

## Support

Report problems in this repository's issue tracker with the App version and the step that failed. Leave out
credentials and any page content you do not want to share.

---

[Browser Use](https://github.com/browser-use/browser-use) · [Browser Harness](https://github.com/browser-use/browser-harness)
