# Phase 2: reviewed, earned authority

The capability ladder is `observe → propose → apply`. Administrators grant tools; review
evidence can deepen authority only inside that allowlist. Business stages remain
separate: no substate dependency or new business vertical is added.

## Runtime contract

- `ai.agent.capability` is unique per agent/tool pair. Revocation preserves history. New
  and upgraded grants start at observe. Writable `agent.tool_ids` remains the source of
  grants; a capability record alone never authorizes a call.
- `ai.agent.call` holds immutable run/sequence, target, arguments, identity, company
  context, capability epoch and policy snapshots. Static plans, LLM choices and approved
  calls use one dispatcher, executing as the agent user.
- `ai.agent.evidence` is append-only. Database uniqueness permits one judgment and one
  undo signal per call. Public ORM callers cannot create evidence, write snapshots,
  lifecycle fields, counters or approval metadata. Context flags carry no internal
  authority. Locks serialize dispatch, configuration and judgment.
- Started runs freeze their agent, plan/goal and execution context. Drafts can select
  execution companies; empty selection uses the execution user's default company.
  Selections cannot exceed that user's company access. The old pending-tool,
  pending-arguments and approver fields are read-only views over the pending call.
  Revocation, identity changes, retargeting or policy changes invalidate pending
  authority. Authorization refusals terminate either path; static execution errors
  terminate, while LLM execution errors remain feedback.
- Observe executes read tools, subject to explicit approval requirements. Writers
  produce observed intent without invoking their target. Assigned reviewers can accept
  or reject the intent; acceptance never executes it.
- Propose executes reads; writers require approval of the exact snapshot. Apply executes
  either, still honoring explicit `requires_approval`.
- Outcomes distinguish observed, refused, failed and dispatched. Historical steps are
  explicitly legacy/unknown and supply no promotion evidence.

## Review and authority

Review requires both membership of the AI reviewer group and assignment to the agent.
Every currently configured agent execution account is excluded, including accounts
executing other agents. Administrators configure grants, reviewers, thresholds and
reasoned manual levels. Execution accounts cannot configure permissions or manufacture
evidence. Runs, calls, evidence and audit rows are visible only to administrators, the
execution account and assigned reviewers.

A reviewer veto forces observe, records a reason and persistently locks automatic
promotion. Only an administrator can clear it. Manual levels require a reason;
irreversible tools may be assigned manually.

## Evidence epochs

Defaults: 20 samples, 90% approval, three consecutive negatives, irreversible guard
enabled. Only the current capability epoch counts:

- Accepted observed intent is positive. Approved proposed calls are positive only after
  successful execution. Rejections are negative.
- Unreviewed successes and technical failures supply no positive judgment. Reapproval
  and undo cannot generate additional positive samples.
- Level changes, veto, revoke/regrant, identity changes, target/policy changes and
  reversal-contract changes start a fresh epoch. Lifetime history is retained. Old
  observations remain reviewable but cannot promote a later epoch.
- A server-owned daily cron locks each capability, advances at most one level, logs
  counts, evidence period and old/new levels, and notifies reviewers. Each transition
  needs a fresh sample set.
- Three consecutive qualifying rejections demote immediately. Any requested undo of an
  applied call removes apply authority immediately, even when reversal fails. Failure
  preserves applied state and exposes repair information.

## Reversal contract

Automatic writing authority at apply requires trusted addon code binding an undo adapter
to the exact model/method target. An editable Boolean is insufficient. Adapters prepare
before execution, capture a receipt after execution and reverse from the receipt,
checking for intervening changes. Initial adapters must operate entirely inside
PostgreSQL and prove atomic execution and reversal. Read tools need no adapter. Writers
without adapters stop at propose; guarded irreversible tools cannot automatically reach
apply even with an adapter.

A test-only vertical demonstrates the complete path. Banking proposals remain
unattributed and earn no credit. Production feedback needs an adapter binding execution
to its originating call; no editable capability-id shortcut exists.

## Upgrade and delivery

Upgrade initializes all existing grants at observe and cancels running and
awaiting-approval runs lacking trustworthy snapshots, with an explanatory error. Drafts,
completed history and audit rows survive. Verify fresh installation, upgrade, static and
scripted LLM paths, forged ORM/context inputs, reviewer permissions,
duplicate/concurrent dispatch and judgment, epoch rules, demotion, undo failure and
unsafe reversal. Preserve banking regression coverage.

Five reviewable commits: snapshots/shared dispatch; capability/grants/migration;
reviewer permissions/evidence; epochs/promotion/demotion/adapters; end-to-end,
concurrency/upgrade tests and documentation. Run Python 3.12 and cached 3.15.0rc3
suites, pre-commit and the fork Odoo/OCB matrix. Report addon counts separately from the
two core web harness entries.

No staging or production deployment. Staging runtime remains unproven. External side
effects have no exactly-once guarantee; automatic-write adapters are restricted to
database-local operations. Nothing goes upstream without human maintainer agreement;
agent review cannot replace it.
