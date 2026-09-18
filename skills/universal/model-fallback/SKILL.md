---
name: model-fallback
description: 'Use when your model keeps erroring — rate-limits, "temporarily limiting requests", overloaded, 429, API 5xx — instead of stalling or burning the session on retries. Defines the cross-harness fallback chain: retry briefly, drop to the next executor, surface which model is active, return to the top once it recovers.'
---

# Fall down a model chain on repeated errors; don't stall on a throttle

When the model you're running on keeps throwing **transient errors** — rate-limit,
"temporarily limiting requests", overloaded, 429, 5xx — the reflex is to retry forever, or
to give up and let the task die, or to "reduce fan-out" and hope. All three are wrong. A
transient throttle on one provider is exactly what a *different provider's quota* can serve
right now. So retry a few times, and if it doesn't clear, **fall to the next executor on
the chain** and keep working.

## The chain

```
claude:fable  ->  claude:opus  ->  oc:GLM-5.2  ->  codex:gpt5.5  ->  omp:k3
```

`<harness>:<model>`, strongest/preferred first. The chain deliberately **crosses
harnesses** so a whole-provider outage (an Anthropic throttle) doesn't wedge everything:

- `claude:fable` -> `claude:opus` — an in-harness **model swap** (still Claude, new model).
- `claude` -> `oc:GLM-5.2` — **cross-harness**: re-dispatch the unit of work to opencode as
  an executor (`opencode run`), on a different provider's quota.
- `oc` -> `codex:gpt5.5` — **cross-harness**: re-dispatch to codex (`codex exec`).
- `codex` -> `omp:k3` — **cross-harness**: re-dispatch to omp (the daily-driver harness,
  k3 on the Kimi-for-Coding endpoint), the last resort.

This is the ONE chain. It lives in `lib/contracts/models.yaml` under `fallback_chain:` and
is read by every harness — cc, codex, opencode, pi, omp — so they all agree on the order. Don't
hand-copy a different order anywhere; change the manifest and every harness follows.

## The discipline

1. **Classify the failure first.** Only a *transient model error* triggers a fallback:
   rate-limit / overload / 429 / 5xx / "temporarily limiting" / "service unavailable". A
   **normal failure** — a failing test, wrong code, a refusal, a real bug — is NOT a model
   error. Switching executor won't fix it and just burns reserve quota. Fix the actual
   problem; don't fall down the chain for it.

2. **Retry the same model a few times before switching.** A single 429 is noise. Retry with
   backoff (~3 attempts) on the current step. Only *consecutive* transient errors past the
   threshold trigger the drop. (Same retry-then-replace policy as the review-cli resilience
   work — retry the seat, then promote a reserve.)

3. **On the Nth repeated error, DROP to the next entry.** Don't keep hammering a throttled
   provider. Advance one step: swap the model if the next step is the same harness, or
   re-dispatch the unit of work to the next harness if it crosses the boundary.

4. **Surface which model is now active.** Say it out loud — "claude:fable is throttled,
   falling to claude:opus" / "...re-dispatching to oc:GLM-5.2". A silent switch hides why
   output style/latency changed and makes a stuck chain undiagnosable.

5. **Return to the top when the preferred model recovers.** A throttle is transient by
   definition. After a successful turn, promote back toward the preferred model — don't pin
   the rest of the session on the last-resort executor because of one bad minute.

6. **At the end of the chain, fail loud.** If `omp:k3` is also erroring, there is
   nowhere left to fall. Say so clearly (which executors failed, why) and stop — never
   pretend success or silent-empty (codex `exec` exit-0 lies about completion; verify the
   work actually landed, don't trust the exit code).

## This is automatic, not just prose

The `model-error-fallback` agent-hook (`agent-hooks/model-error-fallback/`) does the
counting and the switching for you: it tallies consecutive transient errors per unit of
work, drops to the next step past the threshold, emits an in-harness swap or a
cross-harness re-dispatch instruction, and promotes back on recovery. rig provisions it into
every harness. The skill is the discipline you follow when you're driving manually or when
the hook surfaces a switch; the hook is the mechanism so it isn't a promise that regresses.

## Quick reference

| Situation | Wrong | Right |
|---|---|---|
| One 429 | Fall to the next model immediately | Retry the same model a few times first |
| Repeated throttle on fable | Keep retrying forever / reduce fan-out / give up | Drop to `claude:opus`, then `oc:GLM-5.2` |
| A test failed | Fall down the chain | It's not a model error — fix the test/code |
| Switched to opus | Switch silently | Say "fable throttled, now on opus" |
| fable recovered | Stay on the last-resort executor | Promote back toward the top |
| Whole chain erroring | Pretend it worked / silent-empty | Fail loud: name what failed, stop |

## Common mistakes

- **Falling for a non-transient failure.** A failing test or a refusal is not a throttle.
  The chain is for *provider health*, not for "the answer is wrong". Classify before you
  switch.
- **Switching on the first error.** One 429 is noise; retry the same model a few times.
  Only repeated, consecutive transient errors past the threshold should drop you.
- **Staying down after recovery.** Forgetting to return to the top pins quality/cost on the
  last-resort executor. The whole point of "transient" is that it passes.
- **A silent switch.** If you don't surface the active model, nobody can tell why behaviour
  changed or that the chain is stuck — defeats the diagnostic value.
- **Trusting a cross-harness exit code.** `codex exec` / `opencode run` can exit 0 without
  doing the work. After a re-dispatch, verify the unit of work actually landed.
- **Copying a divergent chain.** The order lives in `lib/contracts/models.yaml`
  `fallback_chain:`. A second hand-maintained copy drifts; read the manifest.

## Why

The throttle that kills agents mid-task is almost always *transient and provider-local* —
the same request would succeed on a different provider's quota immediately. Stalling, dying,
or cutting fan-out all surrender to a problem the chain solves by routing around it. The
discipline turns a fatal outage into a brief, surfaced, self-healing degradation: retry,
fall, keep working, climb back. Pairs with the transient-rate-limit lesson
(retry-then-fallback, never reduce-fan-out), the review-cli resilience policy (retry the
seat, then promote a reserve), `promise-durable-action` (the hook is the mechanism, not a
verbal "I'll switch next time"), and `task-completion-selfcheck` (after a cross-harness
re-dispatch, confirm the work actually landed before reporting done).
