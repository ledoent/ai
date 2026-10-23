Define a tool, grant it to an agent, then run a plan.

```python
tool = env["ai.agent.tool"].create({
    "name": "count_partners",
    "description": "How many contacts exist.",
    "model_name": "res.partner",
    "method_name": "search_count",
})
agent = env["ai.agent"].create({
    "name": "Reporter",
    "user_id": env.ref("base.user_demo").id,   # never the superuser
    "tool_ids": [(6, 0, tool.ids)],
})
run = env["ai.agent.run"].create({
    "agent_id": agent.id,
    "plan": '[{"tool": "count_partners", "args": {}}]',
})
run.action_start()
run.action_run_to_completion()
run.step_ids.mapped("result")
```

A tool can only be created for a method `/json/2` would actually dispatch to —
the same predicate `ai_agent_surface` uses — so `sudo`, `with_user`, private
methods, classmethods and fields are all rejected when you save the tool, not
when it runs.

## Approval

Set `requires_approval` on a tool and the run stops at `awaiting_approval`
*before* calling it, recording the pending tool and arguments. `action_approve()`
authorises **that one step**; the next gated step stops again.
`action_reject()` cancels the run having changed nothing.

## Background execution

`ai.agent.run._cron_drain()` advances every running run and commits as it goes,
using `_commit_progress` so a long queue is spread across several cron
acquisitions instead of monopolising a worker. The cron record ships
**inactive** — background execution should be a deliberate act, and an always-on
drain in a review database would execute plans nobody asked for.

Note that this does not make runs *concurrent*. One cron definition is one
execution at a time, because `_acquire_one_job` locks a single `ir_cron` row
with `FOR NO KEY UPDATE SKIP LOCKED`. Quantifying that, and whether a partial
drain changes it, is a separate experiment.

## What is deliberately not here

No language model, no planner, no chat surface, no tool-call parsing. Those are
the parts most likely to be redesigned, and none of them can be evaluated
honestly until the gates above are known to hold.
