# pylint: disable=invalid-commit,print-used
# Disposable harness: commits and machine-readable output are intentional.
"""Seed c150796 in a disposable database through odoo shell."""

import json

from odoo.tests import new_test_user


def run(env):
    user = new_test_user(env, login="upgrade_executor", groups="base.group_user")
    tool = env["ai.agent.tool"].create(
        {
            "name": "upgrade_count",
            "description": "Upgrade read fixture",
            "model_name": "res.partner",
            "method_name": "search_count",
        }
    )
    writer = env["ai.agent.tool"].create(
        {
            "name": "upgrade_create",
            "description": "Upgrade pending fixture",
            "model_name": "res.partner",
            "method_name": "create",
            "requires_approval": True,
        }
    )
    agent = env["ai.agent"].create(
        {
            "name": "Upgrade fixture",
            "user_id": user.id,
            "tool_ids": [(6, 0, [tool.id, writer.id])],
        }
    )
    archived = env["ai.agent"].create(
        {
            "name": "Archived upgrade fixture",
            "user_id": user.id,
            "tool_ids": [(4, tool.id)],
            "active": False,
        }
    )
    ids = {"agent": agent.id, "archived": archived.id}
    for state in ("draft", "running", "awaiting_approval", "done"):
        plan = (
            [{"tool": writer.name, "args": {"vals_list": [{"name": "Legacy pending"}]}}]
            if state == "awaiting_approval"
            else [{"tool": tool.name, "args": {"domain": []}}]
        )
        record = env["ai.agent.run"].create(
            {"agent_id": agent.id, "plan": json.dumps(plan)}
        )
        if state != "draft":
            record.action_start()
        if state in ("awaiting_approval", "done"):
            record._advance()
        assert record.state == state
        ids[state] = record.id
    env["ir.config_parameter"].set_str("phase2.upgrade.fixture", json.dumps(ids))
    env.cr.commit()
    print(json.dumps({"seed": ids, "result": "passed"}))


run(globals()["env"])
