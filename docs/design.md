# Dynamic operation + target

The input is a natural-language goal. Every page observation builds an indexed table of accessible elements and their current values. One node receives one index, even when it supports both clicking and typing.

A decision asks one `text.decide` question: which operation to perform. Only `CLICK`, `TYPE_TEXT` and `SELECT` need an element; for those, a second `text.decide` question asks which target to use for that operation, with the same observed state and whatever remains of the same decision budget. `WAIT`, `DONE`, `BLOCKED` and scrolling need no second call. Each target question contains only elements compatible with its operation; dropdown targets include a code-owned option index. When only one element qualifies it is taken without a model question and carries no probability.

The operation and target questions keep the upstream wording: the same operation labels, the same next-step rules, and target criteria that include current values and checked/selected/expanded state. The target question's premise still names the operation it assumes. The request representation is built by one function, `model.decision_representation`, so a more compact representation can replace it for every backend at once.

Before the second call and before any browser input the loop checks that the run is still active. A stop invalidates the running decision: a result that arrives afterwards is discarded, and continuing observes the page again. Browser input that already started is not rolled back.

TYPE_TEXT sends the goal, selected field, visible page context, and recent actions to `text.generate` with a structured output of `{"text": string|null}`. The result must parse to exactly one non-empty `text` value of at most 2,000 characters. The code does not extract quoted literals. A value can be reused after a stale decision only while the entire helper input is identical, and is discarded after a successful mutation.

## Who runs the models

The Python worker never runs a model. It sends bounded `ai_request` messages to the Nimi Host, which executes `text.decide` and `text.generate` and answers with `ai_result`. Nimi chooses the backend, for example TypeSafe Jev in the cloud or a local checkpoint; the worker does not know or branch on it. Python holds no credentials and never sees Runtime endpoints. Every result is validated again in Python: selected ids must be submitted ids, probabilities must be finite, cover exactly the submitted ids, sum to 1 within 0.02, and put the selected id first. Nothing is repaired or invented. Host errors fail closed; a model that cannot take a page blocks the run with that reason.

## Runtime

The worker launches a dedicated Chrome with its own user-data-dir and an ephemeral DevTools port, and attaches Browser Harness to it with `BU_CDP_URL` and a unique daemon name. Browser Harness is configured before it is imported, so inherited connection settings cannot redirect it to another browser. On Windows, Chrome and the daemon start suspended inside one kill-on-close job object, so `close` or any worker exit ends exactly that process tree.

One browser-side DOM snapshot supplies common HTML/ARIA roles, names, values, visible text, and executable targets. A WeakMap gives each actual node a code-owned identity; a Map keeps the live references used for execution. Replaced elements receive new identities, disconnected references are pruned, and navigation starts a new cache. These IDs are not CDP backend node IDs. Geometry is always read again immediately before input.

The model sees visible text. Background focus emulation keeps animation frames running in the owned tab. Screenshots are optional and disabled in library calls by default; the worker enables them for the inspector.

Freshness compares semantic state instead of counting DOM mutations. Before a click/select, guards compare the document, full URL, viewport, safe form values/states, selected target, and nearby form/dialog/row context. Text generation, typing, scrolling, waiting, and completion use a full semantic comparison. The executor rechecks target visibility, enabled state, geometry, and click occlusion. Scoped guards intentionally permit unrelated visible content to change; this is a practical heuristic, not proof that arbitrary page changes are irrelevant to the goal.

Browser mutations are not retried by transport recovery. Completed execution is logged before the next observation, including when that observation encounters a navigation. An interrupted native-select evaluation stops because its change event may already have fired. Typing uses a browser select-all command followed by CDP text insertion, so existing input contents are replaced.

The next observation waits for up to two animation frames or 50 ms after an interaction. Editable ARIA comboboxes instead wait for visible options, capped at 200 ms. This avoids paying for a prediction before autocomplete suggestions arrive. An explicit WAIT remains 100 ms; network loading is never fast-forwarded in a recording.

## What changed after the first demo

The initial prototype used five manually prepared steps and copied quoted strings. That proved finite-choice browser execution but did not demonstrate task decomposition or text generation. The current policy removes that shortcut and uses the original goal throughout. Operation/target distributions replace the old flat-choice/lookahead/Noul arrangement.

The audit also found that treating every INPUT as editable misclassified checkboxes. Editable roles now control TYPE_TEXT availability. Tests cover checkbox/radio/button distinction, invalid operation/target outputs, stale decisions, text-cache invalidation, waits, and final-route verification.

For Nimi, the upstream single TypeSafe request, which asked the operation and every target head at once, became the two-call policy above, uniform for every backend. The HTTP inspector server became the stdin/stdout worker protocol, and the user's Chrome profile became a dedicated automation profile.

## Boundaries

Sixty browser actions and 240 `text.decide` calls bound a run, which allows the same 120 decisions per run as the upstream one-request loop at two calls each. A decision shares a 60-second budget. Up to 250 action candidates are retained; truncated candidates cannot be selected. A page whose decision request exceeds the `text.decide` bounds blocks the run rather than being truncated. The worker serializes commands, bounds and validates every message, and answers `stop` and `state` while a command runs. The fixture server listens only on 127.0.0.1 and serves one page.

The policy is generic, but two websites do not establish broad reliability. Name resolution covers common labels, ARIA references, and text; it is not the browser's full accessibility algorithm. Shadow roots, frames, canvas, uploads, nested scrolling, pop-ups, and complex keyboard interactions can block progress. A valid action can still be wrong. Independent checks, rather than the model's DONE choice, determine whether a preset task succeeded; a custom goal has no independent check.
