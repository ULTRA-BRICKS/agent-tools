# Cross-harness guard contracts

Status: proposed implementation specification; documentation only.
Date: 2026-09-10.
Epic: [agent-tools#718](https://git.hyperide.ai/ultrabricks/agent-tools/issues/718).
Parent: [rig-cli#514](https://git.hyperide.ai/ultrabricks/rig-cli/issues/514).

## Problem and ownership

A guard is not complete when one bridge labels a process correctly or one entry point rejects a command. The required outcome is consistent enforcement across supported harnesses and their actual execution surfaces, with a trustworthy account of attempted exceptions.

Agent-tools remains a portable catalog/library of guard contracts and carriers. Rig provisions those carriers. Harness-cli owns harness protocols and factual session discovery. Telegram owns human-decision transport. Review-cli owns review capacity evidence. This specification does not add a service orchestrator to agent-tools or move those responsibilities into hooks.

### Evidence baseline

[Issue #573](https://git.hyperide.ai/ultrabricks/agent-tools/issues/573) and [PR #620](https://git.hyperide.ai/ultrabricks/agent-tools/pull/620), merge `6194439ea452ffe341683c04da5e787dd34c8ad0`, record delivered detached launchers, argument-forgery stripping and removal of harness-wide exemptions. The previous acceptance text explicitly admitted missing native Codex/OMP child paths and unavailable Codex live proof while checking them complete. This grooming restores those as unfinished work; it does not deny the narrower delivered changes.

Native OMP remains [#712](https://git.hyperide.ai/ultrabricks/agent-tools/issues/712); native Codex remains [#673](https://git.hyperide.ai/ultrabricks/agent-tools/issues/673). The OMP task records an upstream metadata dependency. A reported upstream gap is a blocker to verify, not permission to fabricate identity variables. Narrow label/path work in [#676](https://git.hyperide.ai/ultrabricks/agent-tools/issues/676) remains distinct from the native payload outcome.

Execution coverage is [#692](https://git.hyperide.ai/ultrabricks/agent-tools/issues/692), worktree rebinding is [#693](https://git.hyperide.ai/ultrabricks/agent-tools/issues/693), and audit short-circuit behavior is [#713](https://git.hyperide.ai/ultrabricks/agent-tools/issues/713). These are separate failure classes, not duplicates of native identity.

## Trusted identity

### Required normalized context

Finalize exact field names with the bridge implementation, but the shared context must carry:

| Field/semantic | Requirement |
| --- | --- |
| Harness kind and version | Derived from the actual carrier, not a tool argument |
| Native session ID and generation | Identity that distinguishes restart/reuse |
| Parent identity | Proven native parent relation or a sanctioned launcher relation |
| Invocation identity | Binds the context to this tool call, including concurrent siblings |
| Agent role | main, child or unknown, supported by provenance |
| Trust source | Native event/record or the defined launcher boundary; unavailable is explicit |
| Repository/worktree binding | Canonical checkout root validated for this operation |

An environment variable in a trusted launcher can be one source in its documented threat model. An arbitrary value supplied by a calling model is not inherently trusted. A process-global variable cannot correctly distinguish native children sharing a process. Path depth, a label suffix, recent transcript activity and PID alone cannot establish the native parent relationship.

### Alternatives and decision

| Approach | Assessment |
| --- | --- |
| Exempt an entire harness | Rejected by the original user requirement; removes policy rather than implementing identity |
| Infer role from path depth/labels | Useful diagnostic hints, insufficient authority; brittle for grandchildren and shared processes |
| Launcher context only | Retain for real detached launchers, but incomplete for native spawn/task calls |
| Native per-call identity normalized with launcher identity | Target contract; requires confirmed metadata and explicit unsupported cases |

The bridge must strip identity-like tool arguments before normalization and must not let later argument merges overwrite trusted context. Unknown identity retains the relevant protections and emits a harness-specific remediation/blocker; it must not grant a global exemption.

### Conformance matrix

For each supported carrier, test main, launcher child, native child, grandchild and concurrent siblings against each subagent-aware hook. Include forged argument, missing metadata, reused PID/session and an unrelated caller's environment. Fixture provenance and harness versions belong in the test documentation. A fake child fixture invented by the bridge author is not evidence of native support.

[#573](https://git.hyperide.ai/ultrabricks/agent-tools/issues/573) closes only when both #712 and #673 meet the shared contract and separately required live checks exist. Quota-blocked live verification remains unchecked. Do not rerun charged or live sessions merely to validate this documentation.

## Enforcement surfaces and worktree binding

[#692](https://git.hyperide.ai/ultrabricks/agent-tools/issues/692) owns consistent protected-operation enforcement for Bash, Edit/Write and Eval/Workflow where the carrier exposes those surfaces. Classify the operation and target at an enforceable boundary. A few source-code regexes can be a preventive rule, but cannot be claimed as a complete execution sandbox.

The implementation decision must compare the actual available carrier hooks, trusted execution/write wrappers and capability restrictions. Where a surface cannot be intercepted, report that limitation and choose an explicit policy rather than silently allowing it. Preserve legitimate read-only inspection and sanctioned review/reporting flows.

[#693](https://git.hyperide.ai/ultrabricks/agent-tools/issues/693) owns checkout identity after an execution-kernel restart. A process restart must not reset a worktree-bound operation to an unrelated primary checkout. Resolve relative targets against the trusted operation context; canonicalize paths and check protected repository targets consistently. Revalidate on restart and when target context changes. The non-repository target fix [#698](https://git.hyperide.ai/ultrabricks/agent-tools/issues/698) is useful narrow work, not proof that Eval-mediated repository writes are covered.

Required negative tests: a main-checkout protected write through every exposed surface is blocked; an authorized worktree operation succeeds; read-only inspection remains possible; restarting the execution kernel cannot change the protected root; a forged worktree argument cannot authorize another checkout. Tests use temporary repositories and do not modify a real user's primary checkout.

## Approval identity and delivery

The producer and Telegram consumer must share a request contract. A approval decision is bound to the reviewed action, not simply the hook name. The action identity includes a stable request ID or agreed fingerprint of exact command/argv, relevant repository/worktree scope, origin session generation and policy. Present an accurate human-readable description of that scope. Do not omit destructive suffixes, truncate the reviewed action silently or accept a blank question.

[agent-tools#702](https://git.hyperide.ai/ultrabricks/agent-tools/issues/702) consumes actual outcomes: posted, not posted/dropped, unanswered/expired, explicit denial, approval and transport failure. These are distinct facts, not a single Boolean decline. Existing [tg-cli#372](https://git.hyperide.ai/ultrabricks/tg-cli/issues/372) is producer-side progress; each hook consumer still needs integration acceptance.

Late decisions may be retained, but a changed command, repository or origin is a new authorization request. Reusing the same hook name must not transfer a one-use decision. The acceptance matrix in [tg-cli#380](https://git.hyperide.ai/ultrabricks/tg-cli/issues/380) covers this producer/consumer boundary. A timeout, unknown outcome or missing record is never approval.

## Audit without side effects

[#713](https://git.hyperide.ai/ultrabricks/agent-tools/issues/713) must preserve first-block enforcement semantics while making every requested hatch visible.

Recommended separation:

1. Read and normalize all requested hatch declarations for this invocation, recording attempts with correlation IDs. This phase must not prompt the human or execute the protected operation.
2. Evaluate enforcement in its established order and stop side-effecting processing on the first block.
3. Record the block and mark other attempts as not evaluated because of that block. Do not falsely label them denied or approved.
4. A later explicitly approved retry links to the original scoped attempt and records consumption once.

Do not solve missing audit rows by running every remaining hook after a block: those hooks may send approval cards or have other side effects. Tests with two requested hatches and a first-hook block must observe both audit entries, exactly one relevant enforcement evaluation, zero later approval prompts and zero protected operations.

Audit records should contain invocation ID, hook ID, scoped request reference, requested/evaluated/decision/consumption state, reason and timestamp. Redact secrets from command context and never store credential values merely because they occur in an environment variable.

## Review-capacity exception boundary

[#705](https://git.hyperide.ai/ultrabricks/agent-tools/issues/705) consumes [review-cli#472](https://git.hyperide.ai/ultrabricks/review-cli/issues/472). A capacity-related exception is allowed only under the existing authorized policy and fresh, well-formed, complete affirmative evidence. Nonempty candidate coverage is mandatory. Missing CLI, nonzero query exit, malformed JSON, stale data, partial mapping or unknown seats are not proof that every candidate is exhausted.

The exception must be bound to the attempted repository/review context and recorded in audit. It must not bypass unrelated CI, security, review-thread, protected-branch or version gates. No raw merge, no-verify or blanket environment bypass is part of this specification.

## Delivery and closure

P0: protected execution/write surfaces and approval scope. P1: native identity, root binding, audit and accurate decision/capacity consumers. P2: quoting and warning UX (#707/#701), without regressing real refusals.

Every implementation task includes a failing-before/passing-after regression where practical, carrier/consumer integration, exact revision, commands/results and durable artifacts. Prior test counts describe prior runs only. Local `/tmp` paths, a force-checked box and a statement that work is deferred do not establish completed acceptance. Keep narrow completed fixes credited, with parent links explaining what remains.
