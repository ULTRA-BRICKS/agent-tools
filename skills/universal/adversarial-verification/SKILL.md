---
name: adversarial-verification
description: 'Use when verifying a fix or finding — before claiming "works", "no longer happens", or "always works". Don''t confirm the happy path; try to break it. Build the case where it MUST fail, hunt the counterexample, and prove it with an artifact (output, test run, screenshot) — not "correct by construction".'
---

# Prove it from the opposite side

Confirming that a fix works is the weakest test you can run — you go looking for the
case that passes, you find it, you stop. That's how a fix ships that only ever worked
on the one input you tried. Verification is not "show it works"; it's *try to break it
and fail*. Until you've attacked the claim and it survived, you haven't verified it —
you've reassured yourself.

## Rule

When you verify a fix or a finding, do the opposite of confirming it:

- **Attack the claim.** Build the input where it *must* fail — empty, null, boundary,
  the largest, the malformed, the concurrent. Actively construct the counterexample,
  don't wait for one to wander in.
- **A "no longer happens" / "always works" claim demands a refutation attempt.** Any
  universal claim ("always", "never", "no longer", "every case") is only earned by
  trying to refute it. If you find a counterexample, decide: is it an intended boundary
  or a real bug? If you don't, document *which* cases you actually tried.
- **Proof is an artifact, not a story.** "The helper returns empty", "it's correct by
  construction", "the code clearly does X" are not proofs — they're restated intent.
  A proof is something you can attach: the rendered DOM, the generated output, the test
  run with its assertion, the screenshot. Walk the *real* path (render / generate /
  execute end-to-end), not just the unit boundary the bug might never reach.
- **Name the floor.** State explicitly where the fix does *not* hold — the edge cases,
  the inputs it punts on. A documented boundary is honest; a silent "universally
  works" is a lie waiting to be found in production.

## The two-sided check

For any claim, run both sides — the positive proof *and* the refutation attempt:

```text
// Claim: "after the fix, <Badge/> always renders its children"

// (1) Positive — prove via the real rendered output, not a unit return:
render(<Badge>hi</Badge>)
assert dom.text == "hi"      // proof = the actual DOM, not "the helper returned 'hi'"

// (2) Adversarial — find the input where it is OBLIGED to fail:
render(<Badge>{null}</Badge>)
assert dom.text == ""        // counterexample: "always" is refuted
                             // → decide: intended boundary (floor) or bug?
```

Only side (1) is "it works on the case I picked". Side (2) is verification.

## Where this applies

- **Bug fixes** — reproduce the original failure first, then attack the fix with the
  neighbors of the failing input, not just the one ticket case.
- **"It's gone now" claims** — a flake, a race, a leak said to be fixed. Try to make it
  recur (more load, more iterations, the exact timing) before believing it.
- **Findings in a review or investigation** — before you assert "this branch is
  unreachable" or "this value is never null", construct the path that reaches it.

## Why

Confirmation bias is the default failure mode of self-verification: you test toward the
answer you want. The cheapest counter is to invert the goal — make breaking it the win
condition. What survives a genuine attempt to break it is verified; what you merely
failed to break by not trying is not.

The companion habits: separate the worker from the critic so the attacker isn't the
author (`gan-critic-loop`), and for anything user-visible, capture and *look at* the
real render rather than trusting the code (`visual-proof-cycle`).
