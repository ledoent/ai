An administrator defines tools and assigns them to an agent with a dedicated
execution user. Never use the superuser. New grants start at **observe**.

```python
tool = env["ai.agent.tool"].create({
    "name": "count_partners",
    "description": "How many contacts exist.",
    "model_name": "res.partner",
    "method_name": "search_count",
})
agent = env["ai.agent"].create({
    "name": "Reporter",
    "user_id": execution_user.id,
    "tool_ids": [(6, 0, tool.ids)],
    "reviewer_ids": [(6, 0, reviewer.ids)],
})
run = env["ai.agent.run"].create({
    "agent_id": agent.id,
    "plan": '[{"tool": "count_partners", "args": {"domain": []}}]',
})
run.action_start()
run.action_run_to_completion()
```

Choose execution companies on the draft run, or leave them empty to use the
execution user's default company. Starting freezes the agent, plan or goal,
execution identity and company context. Calls always execute as that identity.
The tool must be remotely callable and remain on the current administrator
allowlist. Removing a tool revokes its capability without deleting history.

## Review and authority

Reviewers need the **AI reviewer** group and assignment on the agent. Accounts
configured as any agent's execution identity cannot review or configure
permissions, even when assigned. Administrators, execution users and assigned
reviewers can access the run's invocation and audit records.

- **Observe:** read tools execute. Writers only record intent. Accepting an
  observed intent records agreement and never executes the target.
- **Propose:** writers wait for approval of their exact stored invocation.
- **Apply:** calls execute, still honoring a tool's explicit `requires_approval`.

Explicit approval also applies to read tools at every level. Approvals bind the
snapshot, including arguments, identity and policy. A stale grant, identity,
target or policy produces a refusal; edit a new draft rather than the old run.
Public ORM calls cannot set approval metadata or alter snapshots and evidence.

The **Agent invocations** menu exposes review and undo actions. Open a capability
from the agent form to veto it or, as an administrator, set a manual level or
clear a promotion lock. These transitions require a reason. Veto forces observe
and locks automatic promotion until an administrator clears it.

## Automatic promotion and undo

The daily evaluation cron uses the configured defaults of **20 samples, 90%
approval and three consecutive rejections**, with the irreversible guard on.
Accepted observations count positively. Approved calls count positively only
after successful execution. Technical failures and unreviewed successes give no
positive credit. Each call has at most one judgment and one undo signal.

Each authority transition starts a fresh evidence epoch. The same samples
cannot buy both promotions. Three consecutive qualifying rejections demote
immediately. A requested undo removes apply authority before attempting reversal;
failure leaves the action applied, with an error for manual repair.

Writing tools require trusted addon code implementing the exact target's
`ReversalAdapter` contract before automatic promotion to apply. No administrator
Boolean substitutes for an adapter. Initial adapters are database-local and
must detect intervening changes; the addon ships only a test vertical. Banking
proposal feedback is not attributed to runtime capabilities and gives no credit.

## Background execution and upgrade

The execution drain cron ships **inactive**. Enable it deliberately to advance
running runs. Promotion evaluation is daily and server-owned. PostgreSQL locks
and uniqueness serialize dispatch and judgment; callers encountering a
serialization conflict must retry the transaction, as Odoo RPC does.

Upgrade initializes existing grants at observe and cancels running or pending
legacy runs because their inputs lack trustworthy snapshots. Drafts and completed
history survive; old audit steps are designated legacy and earn no evidence.
External side effects carry no exactly-once guarantee. See
[the capability contract](../docs/capability-ladder.md)
for adapter and evidence details.
