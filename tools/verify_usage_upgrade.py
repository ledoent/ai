# pylint: disable=print-used
"""Seed and verify a Phase 3 run across installation of ai_agent_usage.

Run once through ``odoo shell`` before installing the usage addon, then again
after the upgrade command. The fixture is intentionally committed so both
shell processes observe it.
"""

import json

from odoo import Command
from odoo.tests import new_test_user

AGENT_NAME = "Phase 4 historical usage fixture"
GOAL = "phase4 historical run must remain unmetered"


def main(env):
    Agent = env["ai.agent"]
    Run = env["ai.agent.run"]
    agent = Agent.search([("name", "=", AGENT_NAME)], limit=1)
    if not agent:
        company = env.company
        provider = env["ai.agent.provider"].create(
            {
                "name": AGENT_NAME,
                "kind": "stub",
                "model": "phase4-legacy-marker",
            }
        )
        user = new_test_user(
            env,
            login="phase4_usage_legacy_agent",
            groups="base.group_user,base.group_partner_manager",
        )
        user.write({"company_ids": [Command.set([company.id])]})
        Tool = env["ai.agent.tool"]
        tool = Tool.search(
            [("model_name", "=", "res.partner"), ("method_name", "=", "create")],
            limit=1,
        )
        if not tool:
            tool = Tool.create(
                {
                    "name": "phase4_legacy_create_partner",
                    "description": "Write a partner for an upgrade fixture.",
                    "model_name": "res.partner",
                    "method_name": "create",
                }
            )
        agent = Agent.create(
            {
                "name": AGENT_NAME,
                "user_id": user.id,
                "reviewer_ids": [Command.set([env.uid])],
                "tool_ids": [Command.set([tool.id])],
                "provider_id": provider.id,
            }
        )
        env.ref("ai_agent_runtime.group_ai_reviewer").user_ids |= env.user
        run = Run.create({"agent_id": agent.id, "goal": GOAL, "plan": "[]"})
        run.action_start()
        run.with_context(
            stub_script=[
                {
                    "kind": "tool",
                    "tool": tool.name,
                    "args": {"name": "Must not be executed"},
                },
                {"kind": "final", "answer": "legacy Phase 3 result"},
            ]
        ).action_run_to_completion()
        call = run.call_ids[:1]
        call.action_accept("Phase 3 evidence fixture before usage tracking")
        env.cr.commit()
        print(
            json.dumps(
                {
                    "stage": "seeded_before_usage_install",
                    "run_id": run.id,
                    "run_state": run.state,
                    "call_id": call.id,
                    "evidence_id": call.evidence_ids[:1].id,
                    "agent_id": agent.id,
                }
            )
        )
        return

    run = Run.search([("agent_id", "=", agent.id), ("goal", "=", GOAL)], limit=1)
    assert run and run.state == "done", "The historical run must survive upgrade."
    assert len(run.call_ids) == 1, "The historical tool call must survive upgrade."
    call = run.call_ids
    assert len(call.evidence_ids) == 1, "The capability evidence must survive upgrade."
    assert "ai.agent.usage.attempt" in env.registry.models, (
        "Install the usage addon first."
    )
    attempts = env["ai.agent.usage.attempt"].search_count([("run_db_id", "=", run.id)])
    assert attempts == 0, "Pre-activation calls must not be backfilled."
    assert not env.company.ai_usage_enabled, "Budgets must remain off by default."
    assert not env.company.ai_usage_tracking_started_at, (
        "Tracking begins on first usage."
    )
    print(
        json.dumps(
            {
                "stage": "verified_after_usage_install",
                "run_id": run.id,
                "run_state": run.state,
                "call_id": call.id,
                "evidence_id": call.evidence_ids.id,
                "attempt_count": attempts,
                "tracking_started_at": False,
                "budgets_enabled": False,
            }
        )
    )


main(globals()["env"])
