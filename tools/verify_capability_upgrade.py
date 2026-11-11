# pylint: disable=invalid-commit,print-used
# Disposable harness: commits and machine-readable output are intentional.
"""Assert the baseline-to-ladder migration on the seeded disposable database."""

import json


def run(env):
    ids = json.loads(env["ir.config_parameter"].get_str("phase2.upgrade.fixture"))
    Run = env["ai.agent.run"]
    for key in ("running", "awaiting_approval"):
        record = Run.browse(ids[key])
        assert record.state == "cancelled" and "snapshot" in record.error
        assert not record.call_ids
    assert Run.browse(ids["draft"]).state == "draft"
    done = Run.browse(ids["done"])
    assert done.state == "done" and len(done.step_ids) == 1
    assert done.step_ids.dispatch_state == "legacy" and not done.step_ids.call_id
    caps = env["ai.agent.capability"].search([])
    assert len(caps) == 3 and all(
        c.granted and c.level == "observe" and c.epoch == 1 for c in caps
    )
    assert not env["ai.agent.evidence"].search_count([])
    draft = Run.browse(ids["draft"])
    draft.action_start()
    draft._advance()
    assert draft.state == "done" and draft.call_ids.state == "dispatched"
    print(
        json.dumps(
            {
                "grants": len(caps),
                "cancelled": 2,
                "history_preserved": True,
                "result": "passed",
            }
        )
    )


run(globals()["env"])
