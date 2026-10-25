# pylint: disable=print-used
"""Race two real PostgreSQL dispatches for the same reconciliation proposal.

Run only against a disposable database with `odoo shell`. Fixture creation is
committed before workers open separate cursors, so row locks and the database
uniqueness constraint are exercised with independent transactions.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from psycopg2.errors import SerializationFailure, UniqueViolation

from odoo import SUPERUSER_ID, api, fields
from odoo.tests import Command, new_test_user


def _race_dispatch(registry, run_ids):
    barrier = threading.Barrier(len(run_ids), timeout=20)

    def worker(run_id):
        for attempt in range(4):
            try:
                with registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    cr.execute("SELECT id FROM ai_agent_run WHERE id = %s", [run_id])
                    if not attempt:
                        barrier.wait()
                    run = env["ai.agent.run"].browse(run_id)
                    run._advance()
                    cr.commit()
                    return run.state
            except (SerializationFailure, UniqueViolation):
                continue
        raise AssertionError("Concurrent dispatch did not converge")

    with ThreadPoolExecutor(max_workers=len(run_ids)) as pool:
        return list(pool.map(worker, run_ids))


def _account(env, company, name, code, account_type, reconcile=False):
    Account = env["account.account"]
    account = Account.search(
        [("company_ids", "in", [company.id]), ("code", "=", code)], limit=1
    )
    if not account:
        account = Account.create(
            {
                "name": name,
                "code": code,
                "account_type": account_type,
                "reconcile": reconcile,
                "company_ids": [Command.set([company.id])],
            }
        )
    return account


def _prepare_fixture(env):
    company = env.company
    reviewer = env.user
    env.ref("ai_agent_runtime.group_ai_reviewer").user_ids += reviewer

    receivable = env["account.account"].search(
        [
            ("company_ids", "in", [company.id]),
            ("account_type", "=", "asset_receivable"),
            ("reconcile", "=", True),
        ],
        limit=1,
    ) or _account(
        env, company, "Concurrency Receivable", "998101", "asset_receivable", True
    )
    income = env["account.account"].search(
        [("company_ids", "in", [company.id]), ("account_type", "=", "income")],
        limit=1,
    ) or _account(env, company, "Concurrency Income", "998102", "income")
    cash = env["account.account"].search(
        [("company_ids", "in", [company.id]), ("account_type", "=", "asset_cash")],
        limit=1,
    ) or _account(env, company, "Concurrency Bank", "998103", "asset_cash")
    suspense = _account(
        env, company, "Concurrency Suspense", "998104", "asset_current", True
    )

    partner = env["res.partner"].create(
        {"name": "Phase 3 concurrency customer", "customer_rank": 1}
    )
    partner.with_company(company).property_account_receivable_id = receivable
    journal = env["account.journal"].search(
        [("company_id", "=", company.id), ("type", "=", "bank")], limit=1
    )
    if journal:
        journal.write(
            {
                "default_account_id": cash.id,
                "suspense_account_id": suspense.id,
            }
        )
    else:
        journal = env["account.journal"].create(
            {
                "name": "Phase 3 Concurrency Bank",
                "code": "P3RACE",
                "type": "bank",
                "company_id": company.id,
                "default_account_id": cash.id,
                "suspense_account_id": suspense.id,
            }
        )

    invoice = env["account.move"].create(
        {
            "move_type": "out_invoice",
            "partner_id": partner.id,
            "invoice_date": fields.Date.today(),
            "invoice_line_ids": [
                Command.create(
                    {
                        "name": "Phase 3 concurrency invoice",
                        "quantity": 1,
                        "price_unit": 100,
                        "account_id": income.id,
                    }
                )
            ],
        }
    )
    invoice.action_post()
    candidate = invoice.line_ids.filtered(
        lambda line: line.account_id.account_type == "asset_receivable"
    )
    statement = env["account.bank.statement.line"].create(
        {
            "journal_id": journal.id,
            "date": fields.Date.today(),
            "payment_ref": invoice.name,
            "partner_id": partner.id,
            "amount": 100,
        }
    )

    user = env["res.users"].search(
        [("login", "=", "phase3_recon_race_executor")], limit=1
    ) or new_test_user(
        env,
        login="phase3_recon_race_executor",
        groups=(
            "base.group_user,base.group_partner_manager,account.group_account_invoice"
        ),
    )
    user.write({"company_ids": [Command.set([company.id])]})
    company.write(
        {
            "ai_recon_agent_enabled": True,
            "ai_recon_reviewer_ids": [Command.set([reviewer.id])],
        }
    )

    Tool = env["ai.agent.tool"]
    tool = Tool.search(
        [
            ("model_name", "=", "ai.recon.match.service"),
            ("method_name", "=", "match_items"),
        ],
        limit=1,
    ) or Tool.create(
        {
            "name": "phase3_race_match_items",
            "description": "Create a reconciliation proposal for a race check.",
            "model_name": "ai.recon.match.service",
            "method_name": "match_items",
        }
    )
    agent = env["ai.agent"].create(
        {
            "name": "Phase 3 reconciliation race fixture",
            "user_id": user.id,
            "reviewer_ids": [Command.set([reviewer.id])],
            "tool_ids": [Command.set([tool.id])],
        }
    )
    agent._capability(tool).action_set_level(
        "apply", "Database-local proposal creation race check"
    )

    run_ids = []
    for _index in range(2):
        run = env["ai.agent.run"].create(
            {
                "agent_id": agent.id,
                "company_ids": [Command.set([company.id])],
                "plan": json.dumps(
                    [
                        {
                            "tool": tool.name,
                            "args": {
                                "statement_line_id": statement.id,
                                "candidate_line_ids": candidate.ids,
                            },
                        }
                    ]
                ),
            }
        )
        run.action_start()
        run_ids.append(run.id)

    env.cr.commit()
    return statement.id, run_ids


def main(env):
    statement_id, run_ids = _prepare_fixture(env)
    outcomes = _race_dispatch(env.registry, run_ids)
    with env.registry.cursor() as cr:
        check_env = api.Environment(cr, SUPERUSER_ID, {})
        statement = check_env["account.bank.statement.line"].browse(statement_id)
        matches = check_env["ai.recon.match"].search(
            [("statement_line_id", "=", statement_id)]
        )
        runs = check_env["ai.agent.run"].browse(run_ids)
        states = runs.mapped("state")
        calls = runs.call_ids
        print(
            json.dumps(
                {
                    "race_outcomes": outcomes,
                    "run_states": states,
                    "call_states": calls.mapped("state"),
                    "call_results": calls.mapped("result"),
                    "match_ids": matches.ids,
                },
                default=str,
            )
        )
        assert len(matches) == 1, matches.ids
        assert sorted(states) == ["done", "failed"], states
        assert sorted(calls.mapped("state")) == ["dispatched", "failed"]
        assert "live match proposal" in (
            calls.filtered(lambda call: call.state == "failed").result or ""
        )
        assert not statement.is_reconciled
    assert sorted(outcomes) == ["done", "failed"], outcomes
    print(
        json.dumps(
            {
                "database": env.cr.dbname,
                "statement_line_id": statement_id,
                "run_ids": run_ids,
                "outcomes": outcomes,
                "live_matches": len(matches),
                "result": "passed",
            }
        )
    )


main(globals()["env"])
