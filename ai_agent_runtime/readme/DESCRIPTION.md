A durable agent runtime with an administrator tool allowlist, protected invocation
snapshots and the reviewed **observe → propose → apply** capability ladder.
Static plans and model-driven choices share one dispatch boundary. Calls execute
with a frozen company context and the agent user's access rights.

Observed writing intents never invoke their target. Assigned human reviewers can
accept them without execution, approve proposed calls or veto a capability.
Append-only, deduplicated evidence supports daily promotion inside existing tool
grants. Fresh evidence is required after every authority change; rejections and
undo requests remove authority immediately.

Automatic writing authority requires trusted database-local reversal adapters
bound to the exact target. The runtime ships a test-only adapter fixture, not a
production business adapter. External calls have no exactly-once guarantee.
