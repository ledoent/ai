```python
provider = env["ai.agent.provider"].create({
    "name": "Claude",
    "kind": "anthropic",
    "model": "claude-sonnet-5",
    "api_key_env": "ANTHROPIC_API_KEY",
})
agent.write({"provider_id": provider.id, "max_steps": 12})

run = env["ai.agent.run"].create({
    "agent_id": agent.id,
    "goal": "How many unpaid customer invoices are there, and what do they total?",
})
run.action_start()
run.action_run_to_completion()
print(run.answer)
run.step_ids.mapped(lambda s: (s.tool_id.name, s.args, s.result))
```

Set a `goal` and the run is model-driven; leave it empty and the run follows its
`plan` exactly as before. Both paths share the same gates, step records and
resume cursor.

## Tool schemas

`tool._tool_schema()` derives the parameter names, their JSON types and any
reST `:param:` text straight from the method's real signature. The
**description stays hand-written**, because that is the part derivation cannot
supply: Odoo's docstrings explain mechanics to a developer who has already
decided to call the method, not when an agent should reach for it.

So "derive the schema or hand-write it" is a false choice — derive the shape,
write the description.

## Testing without a provider

```python
run = run.with_context(stub_script=[
    {"kind": "tool", "tool": "count_partners", "args": {"domain": []}},
    {"kind": "final", "answer": "42"},
])
```

A `stub` provider replays that list, indexed by how many steps have happened.
The script travels in the context so the stub needs no column of its own.
