import json

from odoo.tests import TransactionCase, new_test_user


class AgentRuntimeCommon(TransactionCase):
    """An agent acting as a deliberately narrowly-privileged user.

    The agent's user can read *and create* contacts but cannot touch
    ``ir.mail_server``. That asymmetry is what lets the tests tell "executed as
    the agent's user" apart from "executed as sudo" -- the distinction the whole
    design rests on.

    ``base.group_partner_manager`` is required and not incidental:
    ``base.group_user`` alone has only **read** on ``res.partner``
    (``base/security/ir.access.csv:54``), with create granted to
    ``group_partner_manager`` (``:56``). Worth knowing when provisioning a real
    agent's user -- the obvious choice of "just an internal user" cannot create
    a contact, and the resulting failure looks like a broken tool rather than a
    missing group.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent_user = new_test_user(
            cls.env,
            login="agent_runner",
            groups="base.group_user,base.group_partner_manager",
        )
        cls.Tool = cls.env["ai.agent.tool"]
        cls.Run = cls.env["ai.agent.run"]

        cls.tool_count = cls.Tool.create(
            {
                "name": "count_partners",
                "description": "How many contacts exist.",
                "model_name": "res.partner",
                "method_name": "search_count",
            }
        )
        cls.tool_create = cls.Tool.create(
            {
                "name": "create_partner",
                "description": "Create a contact.",
                "model_name": "res.partner",
                "method_name": "create",
                "requires_approval": True,
            }
        )
        # Defined but NOT granted to the agent below -- the allowlist fixture.
        cls.tool_ungranted = cls.Tool.create(
            {
                "name": "count_users",
                "description": "How many users exist.",
                "model_name": "res.users",
                "method_name": "search_count",
            }
        )
        # Targets a model the agent's user cannot read at all.
        cls.tool_forbidden = cls.Tool.create(
            {
                "name": "count_mail_servers",
                "description": "How many outgoing mail servers exist.",
                "model_name": "ir.mail_server",
                "method_name": "search_count",
            }
        )

        cls.agent = cls.env["ai.agent"].create(
            {
                "name": "Test Agent",
                "user_id": cls.agent_user.id,
                "tool_ids": [
                    (
                        6,
                        0,
                        [
                            cls.tool_count.id,
                            cls.tool_create.id,
                            cls.tool_forbidden.id,
                        ],
                    )
                ],
            }
        )

    def make_run(self, *steps):
        """Create a started run from ``(tool_name, args)`` pairs."""
        plan = [{"tool": name, "args": args} for name, args in steps]
        run = self.Run.create({"agent_id": self.agent.id, "plan": json.dumps(plan)})
        run.action_start()
        return run
