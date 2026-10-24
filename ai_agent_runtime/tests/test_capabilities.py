from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import tagged

from .common import AgentRuntimeCommon


@tagged("post_install", "-at_install")
class TestCapabilities(AgentRuntimeCommon):
    def test_new_grants_are_observe_and_revocation_preserves_history(self):
        self.agent.tool_ids += self.tool_ungranted
        cap = self.agent._capability(self.tool_ungranted)
        self.assertEqual(cap.level, "observe")
        epoch = cap.epoch
        self.agent.tool_ids -= self.tool_ungranted
        self.assertFalse(cap.granted)
        self.agent.tool_ids += self.tool_ungranted
        self.assertTrue(cap.granted)
        self.assertGreater(cap.epoch, epoch)
        self.assertEqual(self.agent._capability(self.tool_ungranted), cap)

    def test_observe_writer_never_invokes_target(self):
        cap = self.agent._capability(self.tool_create)
        cap.action_set_level("observe", "Observe fixture")
        with patch.object(type(self.Run), "_invoke") as invoke:
            run = self.make_run(
                ("create_partner", {"vals_list": [{"name": "Untouched"}]})
            )
            run.action_run_to_completion()
        invoke.assert_not_called()
        self.assertEqual(run.state, "done")
        self.assertEqual(run.call_ids.state, "observed")
        self.assertEqual(run.step_ids.dispatch_state, "observed")

    def test_readonly_explicit_approval_still_applies(self):
        self.tool_count.requires_approval = True
        run = self.make_run(("count_partners", {"domain": []}))
        run._advance()
        self.assertEqual(run.state, "awaiting_approval")
        run.action_approve()
        run._advance()
        self.assertEqual(run.call_ids.state, "dispatched")

    def test_regrant_does_not_restore_pending_authority(self):
        run = self.make_run(("create_partner", {"vals_list": [{"name": "Old grant"}]}))
        run._advance()
        run.action_approve()
        self.agent.tool_ids -= self.tool_create
        self.agent.tool_ids += self.tool_create
        run._advance()
        self.assertEqual(run.call_ids.state, "refused")

    def test_identity_change_invalidates_even_after_restoration(self):
        run = self.make_run(
            ("create_partner", {"vals_list": [{"name": "Old identity"}]})
        )
        run._advance()
        run.action_approve()
        self.agent.user_id = self.env.ref("base.user_admin")
        self.agent.user_id = self.agent_user
        run._advance()
        self.assertEqual(run.call_ids.state, "refused")

    def test_capability_alone_never_grants_a_tool(self):
        self.env["ai.agent.capability"]._new(
            {
                "agent_id": self.agent.id,
                "tool_id": self.tool_ungranted.id,
                "policy": self.tool_ungranted._policy_signature(),
                "level": "apply",
            }
        )
        run = self.make_run(("count_users", {"domain": []}))
        run._advance()
        self.assertEqual(run.call_ids.state, "refused")

    def test_execution_identity_cannot_configure_authority(self):
        for record, vals in (
            (self.agent, {"name": "Forged"}),
            (self.tool_count, {"requires_approval": False}),
            (self.agent._capability(self.tool_count), {"level": "apply"}),
        ):
            with self.assertRaises(AccessError):
                record.with_user(self.agent_user).with_context(internal=True).write(
                    vals
                )
