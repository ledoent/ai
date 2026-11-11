# pylint: disable=print-used
"""Exercise the production durable provider path in Odoo shell."""

import json

from odoo import Command, api
from odoo.tests import new_test_user


def main(env):
    company = env.company
    company.write({"ai_usage_enabled": False, "ai_usage_monthly_limit": 0.0})
    env.cr.commit()
    user = env["res.users"].search(
        [("login", "=", "usage_durable_smoke_executor")], limit=1
    ) or new_test_user(
        env, login="usage_durable_smoke_executor", groups="base.group_user"
    )
    user.write({"company_ids": [Command.set([company.id])]})
    provider = env["ai.agent.provider"].search(
        [("name", "=", "Durable smoke provider")], limit=1
    ) or env["ai.agent.provider"].create(
        {
            "name": "Durable smoke provider",
            "kind": "stub",
            "model": "durable-smoke-model",
        }
    )
    agent = env["ai.agent"].search(
        [("name", "=", "Durable smoke agent")], limit=1
    ) or env["ai.agent"].create(
        {
            "name": "Durable smoke agent",
            "user_id": user.id,
            "provider_id": provider.id,
        }
    )
    run = env["ai.agent.run"].search(
        [
            ("agent_id", "=", agent.id),
            ("goal", "=", "Production durable path smoke test"),
        ],
        limit=1,
    )
    if not run:
        run = env["ai.agent.run"].create(
            {
                "agent_id": agent.id,
                "goal": "Production durable path smoke test",
                "plan": "[]",
            }
        )
        run.action_start()
    run_id = run.id
    env.cr.commit()
    run = env["ai.agent.run"].browse(run_id)
    run.with_context(
        stub_script=[{"kind": "final", "answer": "durable result"}]
    ).action_run_to_completion()
    env.cr.commit()
    with env.registry.cursor() as cr:
        check = api.Environment(cr, env.uid, {})
        persisted_run = check["ai.agent.run"].browse(run_id)
        attempt = (
            check["ai.agent.usage.attempt"]
            .sudo()
            .search([("run_db_id", "=", run_id)], limit=1)
        )
        assert persisted_run.state == "done", (
            f"Unexpected run state: {persisted_run.state}"
        )
        assert attempt and attempt.state == "settled", "The attempt did not settle."
        assert attempt.reservation_state == "released", (
            "Unbudgeted call retained a hold."
        )
        assert attempt.cost_status == "unpriced", "The stub should have unpriced cost."
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "run_state": persisted_run.state,
                    "attempt_uuid": attempt.attempt_uuid,
                    "attempt_state": attempt.state,
                    "reservation_state": attempt.reservation_state,
                    "cost_status": attempt.cost_status,
                }
            )
        )

    company.write({"ai_usage_enabled": True, "ai_usage_monthly_limit": 100.0})
    budgeted_provider = env["ai.agent.provider"].search(
        [("name", "=", "Durable budgeted smoke provider")], limit=1
    ) or env["ai.agent.provider"].create(
        {
            "name": "Durable budgeted smoke provider",
            "kind": "stub",
            "model": "durable-budgeted-smoke-model",
        }
    )
    env["ai.agent.usage.rate"].search(
        [
            ("provider_kind", "=", "stub"),
            ("model", "=", "durable-budgeted-smoke-model"),
        ],
        limit=1,
    ) or env["ai.agent.usage.rate"].create(
        {
            "provider_kind": "stub",
            "model": "durable-budgeted-smoke-model",
            "input_per_million": 1.0,
            "output_per_million": 1.0,
            "currency_id": company.currency_id.id,
            "request_ceiling": 5.0,
        }
    )
    budgeted_agent = env["ai.agent"].search(
        [("name", "=", "Durable budgeted smoke agent")], limit=1
    ) or env["ai.agent"].create(
        {
            "name": "Durable budgeted smoke agent",
            "user_id": user.id,
            "provider_id": budgeted_provider.id,
        }
    )
    budgeted_run = env["ai.agent.run"].search(
        [
            ("agent_id", "=", budgeted_agent.id),
            ("goal", "=", "Production budgeted durable path smoke test"),
        ],
        limit=1,
    )
    if not budgeted_run:
        budgeted_run = env["ai.agent.run"].create(
            {
                "agent_id": budgeted_agent.id,
                "goal": "Production budgeted durable path smoke test",
                "plan": "[]",
            }
        )
        budgeted_run.action_start()
    budgeted_run_id = budgeted_run.id
    env.cr.commit()
    budgeted_run = env["ai.agent.run"].browse(budgeted_run_id)
    budgeted_run.with_context(
        stub_script=[
            {
                "kind": "final",
                "answer": "reported provider cost",
                "cost": {"amount": 1.25, "currency": company.currency_id.name},
            }
        ]
    ).action_run_to_completion()
    env.cr.commit()
    with env.registry.cursor() as cr:
        check = api.Environment(cr, env.uid, {})
        persisted_run = check["ai.agent.run"].browse(budgeted_run_id)
        attempt = (
            check["ai.agent.usage.attempt"]
            .sudo()
            .search([("run_db_id", "=", budgeted_run_id)], limit=1)
        )
        assert persisted_run.state == "done", (
            f"Unexpected budgeted run state: {persisted_run.state}"
        )
        assert attempt and attempt.state == "settled", (
            "The budgeted attempt did not settle."
        )
        assert attempt.reservation_amount == 5.0, (
            "The request ceiling was not reserved."
        )
        assert attempt.charged_amount == 1.25, (
            "The reported provider cost was not charged."
        )
        print(
            json.dumps(
                {
                    "run_id": budgeted_run_id,
                    "run_state": persisted_run.state,
                    "attempt_uuid": attempt.attempt_uuid,
                    "attempt_state": attempt.state,
                    "reservation_amount": attempt.reservation_amount,
                    "charged_amount": attempt.charged_amount,
                    "cost_status": attempt.cost_status,
                }
            )
        )


main(globals()["env"])
