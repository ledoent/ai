Turns the durable runtime into an actual agent: instead of following a plan
written in advance, a model is given the goal and the agent's tool schemas and
chooses one action at a time, seeing each result before deciding the next.

That difference is the point. A static plan has to guess every identifier up
front; a loop can look one up and then use it. It is also where the risk is, so
every control the runtime already had applies unchanged — the model picks
*which* tool, and gets no say in whether it is allowed:

- **The allowlist is not advisory.** A tool the model names but the agent was
  not granted fails the run; an invented tool name does too.
- **Identity is unchanged.** Calls still run as the agent's user, so a tool the
  model chose but the user cannot use simply fails — and the failure goes into
  the transcript, so the model can see it and try something else.
- **Approval executes what the human saw.** After approval the pending call is
  replayed verbatim rather than asking the model again, which would let it
  substitute a different action for the one that was approved.
- **A step budget bounds the loop.** Without one, a model that keeps choosing
  tools runs until something external stops it, and on a production ERP that
  means an unbounded number of writes.

Providers sit behind a one-method interface (`_decide`) because every vendor's
tool-calling API is a different shape and none is a good thing to build control
flow around. A `stub` provider replays a scripted sequence, which is what makes
the loop assertable in CI with no network and no API key.

API keys are read from an environment variable named by the provider record,
never stored in it — a credential in a database column ends up in every backup,
duplicate and support export of that database.
