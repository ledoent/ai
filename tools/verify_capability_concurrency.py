# pylint: disable=invalid-commit,print-used
# Disposable harness: commits and machine-readable output are intentional.
"""Run through `odoo shell` on a disposable installed database, never production.

Unlike TransactionCase, these use independent real PostgreSQL transactions.
Serialization/uniqueness conflicts are retried with a fresh snapshot. The final
assertions inspect committed business values, audit rows and evidence counts.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from psycopg2.errors import SerializationFailure, UniqueViolation

from odoo import SUPERUSER_ID, api
from odoo.exceptions import UserError
from odoo.tests import new_test_user

from odoo.addons.ai_agent_runtime.tests.adapter_fixture import register_fixture


def race(registry, run_id, action):
    barrier = threading.Barrier(2, timeout=20)

    def worker():
        for attempt in range(4):
            try:
                with registry.cursor() as cr:
                    worker_env = api.Environment(cr, SUPERUSER_ID, {})
                    cr.execute("SELECT id FROM ai_agent_run WHERE id = %s", [run_id])
                    if not attempt:
                        barrier.wait()
                    try:
                        action(worker_env, run_id)
                    except UserError:
                        # A second review or undo must be refused after retry.
                        return "refused"
                    cr.commit()
                    return "committed"
            except (SerializationFailure, UniqueViolation):
                continue
        raise AssertionError("Race never converged")

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: worker(), range(2)))
    return outcomes


def run(env):
    registry = env.registry
    register_fixture(env)
    env.ref("ai_agent_runtime.group_ai_reviewer").user_ids += env.user
    user = env["res.users"].search([("login", "=", "concurrency_executor")], limit=1)
    if not user:
        user = new_test_user(
            env, login="concurrency_executor", groups="base.group_user"
        )
    tool = env["ai.agent.tool"].search(
        [
            ("model_name", "=", "ai.runtime.contract.item"),
            ("method_name", "=", "action_set_value"),
        ],
        limit=1,
    ) or env["ai.agent.tool"].create(
        {
            "name": "race_set_value",
            "description": "Database-local race fixture",
            "model_name": "ai.runtime.contract.item",
            "method_name": "action_set_value",
        }
    )
    agent = env["ai.agent"].create(
        {
            "name": "Race fixture",
            "user_id": user.id,
            "tool_ids": [(4, tool.id)],
            "reviewer_ids": [(4, env.uid)],
        }
    )
    item = env["ai.runtime.contract.item"].create({})
    cap = agent._capability(tool)

    def make_run(value):
        result = env["ai.agent.run"].create(
            {
                "agent_id": agent.id,
                "plan": json.dumps(
                    [{"tool": tool.name, "args": {"item_id": item.id, "value": value}}]
                ),
            }
        )
        result.action_start()
        return result

    cap.action_set_level("apply", "Concurrent dispatch fixture")
    dispatch_run = make_run(70)
    env.cr.commit()
    # A dynamically registered fixture is process-local. Acknowledge the schema
    # signal before opening worker Environments so the registry is not rebuilt
    # from installed addons (which deliberately do not register the fixture).
    with registry.cursor() as signaling_cr:
        registry.registry_sequence, _ = registry.get_sequences(signaling_cr)
    dispatch = race(
        env.registry,
        dispatch_run.id,
        lambda e, rid: e["ai.agent.run"].browse(rid)._advance(),
    )
    env.cr.commit()
    env.invalidate_all()
    assert item.value == 70 and item.revision == 1, (
        item.read(["value", "revision"]),
        dispatch_run.call_ids.read(["state", "result"]),
    )
    assert len(dispatch_run.call_ids) == len(dispatch_run.step_ids) == 1

    undo = race(
        env.registry,
        dispatch_run.id,
        lambda e, rid: e["ai.agent.run"].browse(rid).call_ids.action_undo("Race undo"),
    )
    env.cr.commit()
    env.invalidate_all()
    assert item.value == 7 and item.revision == 2
    assert dispatch_run.call_ids.state == "undone"
    assert len(dispatch_run.call_ids.evidence_ids) == 1
    assert cap.level == "propose"

    cap.action_set_level("observe", "Concurrent judgment fixture")
    observed = make_run(90)
    observed._advance()
    env.cr.commit()
    judgment = race(
        env.registry,
        observed.id,
        lambda e, rid: e["ai.agent.run"].browse(rid).call_ids.action_accept(),
    )
    env.cr.commit()
    env.invalidate_all()
    assert len(observed.call_ids.evidence_ids) == 1
    assert cap._statistics()["approvals"] == 1 and item.value == 7

    for index in range(19):
        observation = make_run(index)
        observation._advance()
        observation.call_ids.action_accept()
    env.cr.commit()
    promotion = race(
        env.registry,
        observed.id,
        lambda e, rid: (
            e["ai.agent.run"].browse(rid).call_ids.capability_id._evaluate_promotion()
        ),
    )
    env.cr.commit()
    env.invalidate_all()
    assert cap.level == "propose" and cap._statistics()["approvals"] == 0
    assert (
        len(
            [
                event
                for event in cap.history
                if event["reason"].startswith("Automatic promotion")
            ]
        )
        == 1
    )
    print(
        json.dumps(
            {
                "scenarios": 4,
                "dispatch": dispatch,
                "judgment": judgment,
                "undo": undo,
                "promotion": promotion,
                "result": "passed",
            }
        )
    )


def main(env):
    registry = env.registry
    signal = registry._signal_changes
    # The only patch is test-fixture schema signaling. Execution, locking,
    # transactions and evidence use production code with real connections.
    with patch.object(
        registry,
        "_signal_changes",
        lambda cr, names: signal(cr, set(names) - {"registry"}),
    ):
        run(env)


main(globals()["env"])
