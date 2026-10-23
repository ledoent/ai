A minimal, durable runtime for multi-step AI agent work in Odoo Community: an
agent is an identity plus an allowlist of tools, and a run is a resumable
sequence of tool calls with an approval gate and an append-only audit trail.

There is no model-calling here yet. The plan is written by hand, which keeps the
whole thing deterministic and testable; a planner that produces the plan is a
separate concern and a later addition. What this module establishes is the part
that has to be right *before* a language model is allowed anywhere near it:

- **Two independent gates.** The allowlist decides what the agent may ask for;
  the agent's own user decides what it is permitted to do. Neither substitutes
  for the other, and the tests prove it by varying one at a time.
- **Never `sudo`.** Every tool call runs under `with_user(agent.user_id)`, and
  acting as the superuser is a hard constraint violation rather than a warning.
- **Pause before acting, not after.** A tool marked as needing approval stops
  the run with nothing executed, and one approval authorises exactly one step.
- **Resumable without repeating.** The cursor and the step record are written in
  the same transaction, and a unique constraint on the step sequence makes
  double-execution impossible to record rather than merely unlikely.

Why an allowlist at all, when Odoo already has access rights? Because
`ai_agent_surface` measured that it is not enough: method visibility does not
vary by user, so once a user can read a model every public method on it is
callable. Access rights can say *"may touch contacts"*; they cannot say *"may
read contacts but not delete them"*. That gap is what this module fills.
