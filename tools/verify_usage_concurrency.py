# pylint: disable=invalid-commit,print-used
"""Race provider reservations in independent PostgreSQL transactions.

Run only through ``odoo shell`` on a disposable database with
``ai_agent_usage`` installed. The fixture is committed before worker cursors
start so the company, agent, rate card and runs are visible to every worker.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from psycopg2.errors import SerializationFailure, UniqueViolation

from odoo import SUPERUSER_ID, api
from odoo.tests import Command, new_test_user


def _race_reservations(registry, work):
    barrier = threading.Barrier(len(work), timeout=20)

    def worker(item):
        run_id, rate_id, period, sequence = item
        for attempt in range(4):
            try:
                with registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    run = env["ai.agent.run"].browse(run_id)
                    if not attempt:
                        barrier.wait()
                    if sequence is not None:
                        cr.execute(
                            "SELECT id FROM ai_agent_run WHERE id = %s FOR UPDATE",
                            [run_id],
                        )
                    if sequence is not None:
                        run._set({"step_no": sequence})
                    provider = run.provider_snapshot_id.with_context(
                        _ai_usage_kind_snapshot=run.usage_provider_kind_snapshot,
                        _ai_usage_model_snapshot=run.usage_model_snapshot,
                    )
                    row = env["ai.agent.usage.attempt"]._reserve(
                        run,
                        provider,
                        env["ai.agent.usage.rate"].browse(rate_id),
                        period,
                    )
                    cr.commit()
                    return row["state"]
            except (SerializationFailure, UniqueViolation):
                continue
        raise AssertionError("Concurrent reservations did not converge")

    with ThreadPoolExecutor(max_workers=len(work)) as pool:
        return list(pool.map(worker, work))


def _make_run(env, agent, company):
    run = env["ai.agent.run"].create(
        {
            "agent_id": agent.id,
            "company_ids": [Command.set([company.id])],
            "goal": "Usage race fixture",
            "plan": "[]",
        }
    )
    run.action_start()
    return run


def _write_committed(registry, model_name, record_id, values):
    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        env[model_name].browse(record_id).write(values)
        cr.commit()


def main(env):
    company = env.company
    user = env["res.users"].search(
        [("login", "=", "usage_race_executor")], limit=1
    ) or new_test_user(env, login="usage_race_executor", groups="base.group_user")
    user.write({"company_ids": [Command.set([company.id])]})
    provider = env["ai.agent.provider"].create(
        {"name": "Usage race provider", "kind": "stub", "model": "usage-race-model"}
    )
    agent = env["ai.agent"].create(
        {"name": "Usage race agent", "user_id": user.id, "provider_id": provider.id}
    )
    rate = env["ai.agent.usage.rate"].create(
        {
            "provider_kind": "stub",
            "model": "usage-race-model",
            "input_per_million": 1.0,
            "output_per_million": 1.0,
            "currency_id": company.currency_id.id,
            "request_ceiling": 5.0,
        }
    )
    budget = env["ai.agent.usage.budget"].create(
        {
            "company_id": company.id,
            "agent_id": agent.id,
            "monthly_limit": 0.0,
            "run_limit": 0.0,
        }
    )
    company.write(
        {
            "ai_usage_enabled": True,
            "ai_usage_monthly_limit": 5.0,
            "ai_usage_timezone": "UTC",
        }
    )
    company_case_runs = [_make_run(env, agent, company) for _ in range(2)]
    company_work = [
        (run.id, rate.id, date(2026, 10, 1), None) for run in company_case_runs
    ]
    env.cr.commit()
    company_outcomes = _race_reservations(env.registry, company_work)
    env.cr.rollback()
    env.invalidate_all()

    _write_committed(
        env.registry,
        "res.company",
        company.id,
        {"ai_usage_monthly_limit": 100.0},
    )
    _write_committed(
        env.registry,
        "ai.agent.usage.budget",
        budget.id,
        {"monthly_limit": 5.0},
    )
    env.cr.rollback()
    env.invalidate_all()
    company = env["res.company"].browse(company.id)
    budget = env["ai.agent.usage.budget"].browse(budget.id)
    agent = env["ai.agent"].browse(agent.id)
    agent_case_runs = [_make_run(env, agent, company) for _ in range(2)]
    agent_work = [(run.id, rate.id, date(2026, 11, 1), None) for run in agent_case_runs]
    env.cr.commit()
    agent_outcomes = _race_reservations(env.registry, agent_work)
    env.cr.rollback()
    env.invalidate_all()

    _write_committed(
        env.registry,
        "ai.agent.usage.budget",
        budget.id,
        {"monthly_limit": 0.0, "run_limit": 5.0},
    )
    env.cr.rollback()
    env.invalidate_all()
    company = env["res.company"].browse(company.id)
    budget = env["ai.agent.usage.budget"].browse(budget.id)
    agent = env["ai.agent"].browse(agent.id)
    run_case = _make_run(env, agent, company)
    run_work = [
        (run_case.id, rate.id, date(2026, 12, 1), sequence) for sequence in (0, 1)
    ]
    env.cr.commit()
    run_outcomes = _race_reservations(env.registry, run_work)

    with env.registry.cursor() as cr:
        check = api.Environment(cr, SUPERUSER_ID, {})
        Attempt = check["ai.agent.usage.attempt"]
        usage_rows = Attempt.search(
            [("run_db_id", "in", [run.id for run in company_case_runs])]
        )
        summary = {
            "company_cap": company_outcomes,
            "agent_cap": agent_outcomes,
            "run_cap": run_outcomes,
            "company_held_total": sum(usage_rows.mapped("held_amount")),
            "company_case_states": usage_rows.mapped("state"),
        }
        assert sorted(company_outcomes) == ["blocked", "reserved"], summary
        assert sorted(agent_outcomes) == ["blocked", "reserved"], summary
        assert sorted(run_outcomes) == ["blocked", "reserved"], summary
        assert summary["company_held_total"] == 5.0, summary
        print(json.dumps(summary, default=str))


main(globals()["env"])
