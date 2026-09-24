# Release notes

## 0.1.2

- Reaching the run's action or decision budget ends the run and says which budget; the controls no longer
  stay enabled while every retry fails.
- A page that changes during the slow-motion pause is observed again instead of ending the automatic run.
- A goal the model calls done gets a check mark only when the independent check passed.
- Text with an unpaired surrogate is refused at once instead of leaving the inspector waiting.
- The App exits if it cannot finish starting, instead of running without a window.

## 0.1.1

- The installed App now finds Google Chrome from the location Chrome's installer registers; it no longer
  depends on environment variables that installed Apps do not receive.
- A run stops and says why when the page keeps changing before the chosen action can run, or when three
  actions in a row change nothing, instead of repeating the same choice until its decision budget is used up.
  An automatic run never ends without an outcome.
- Downloads started in the automation Chrome stay in its temporary profile and are removed with the run.

## 0.1.0

First Nimi desktop version of Jev Ultrafast, adapted from
[browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) (MIT) at commit `1231850`.

- Give it one goal and a start page, or pick a preset: two local practice pages and Google Flights
  (stops at visible results and never books).
- Each step is chosen with Nimi `text.decide`: first the operation, then its element when more than one
  qualifies. Text for a field comes from Nimi `text.generate`. You choose the models for both in Nimi;
  the App holds no model keys.
- The App drives its own automation Chrome, which it starts and closes itself with a temporary profile.
  Your own Chrome and its profiles are never used. Google Chrome must be installed.
- Stop takes effect at once: a pending decision is discarded and nothing more runs.
- A DONE choice is the model's claim. Only the unedited presets are checked independently.

Known limits: runs with real models through Nimi have not been measured yet, so there is no speed or
reliability figure for this version. Frames, shadow roots, canvas, uploads, pop-up tabs and complex
keyboard widgets can block a run.
