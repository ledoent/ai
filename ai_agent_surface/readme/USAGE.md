From `odoo shell`, scan as a specific user:

```python
env["ai.agent.surface"].with_user(env.ref("base.user_demo"))._scan()
```

The return value is a plain dict:

```python
{
    "user": "demo",
    "model_count": 37,
    "method_count": 1929,
    "by_class": {"readonly": 558, "writing": 1334, "irreversible": 37},
    "models": {"res.partner": {"search": "readonly", "unlink": "irreversible", ...}},
}
```

Those are the real figures for a minimal `base` + `rpc` + `web` install. **Your
numbers will be much larger** — the count grows with every installed module, and
that is exactly why this ships as a scan rather than as a published figure. Run
it to get yours.

`_log_scan()` does the same and writes the headline figures to the log at INFO
level, which is useful in a container where you cannot easily get a shell.

## Classification

`readonly` and `writing` are read from core, not guessed: a method is read-only
when `/json/2` would route it to a read-only cursor, determined by the same
most-derived-first walk for `_readonly` that the real route uses.

`irreversible` is different, and is the one opinion this module holds. Core has
no marker for "a human would not expect to be able to undo this", so the module
applies a name-based heuristic. The patterns are in `IRREVERSIBLE_PATTERNS` in
`models/ai_agent_surface.py`, in the open, so you can disagree with a specific
entry. Treat `irreversible` as a lower bound on risk, never as an audit.

Two things the heuristic deliberately does **not** call irreversible:
`action_archive`, because `action_unarchive` undoes it; and anything already
classified read-only, because a read-only cursor cannot write whatever the name
suggests. If you add a pattern, `tests/test_classifier.py` will hold you to it —
it asserts every pattern is fully anchored and can actually match a public
method name. An earlier revision shipped a pattern that could never fire, which
silently under-reported the count.

## What it does not tell you

Model and field visibility vary by user; **method visibility does not**. Once a
user can read a model, the set of methods `/json/2` will dispatch to on it is
the same for everyone. So two users' scans differ in which models appear, not in
what is callable on a shared one. That asymmetry is the point worth knowing
before designing an agent permission model on top of access rights alone.
