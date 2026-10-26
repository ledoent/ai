from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import AgentRuntimeCommon


@tagged("post_install", "-at_install")
class TestSnapshots(AgentRuntimeCommon):
    def test_control_fields_cannot_be_forged_even_with_context(self):
        for field, value in (
            ("state", "running"),
            ("approver_id", self.env.uid),
            ("step_no", 8),
            ("pending_args", "{}"),
            ("call_ids", []),
        ):
            with self.subTest(field=field), self.assertRaises(AccessError):
                self.Run.with_context(internal=True, sudo=True).create(
                    {
                        "agent_id": self.agent.id,
                        field: value,
                    }
                )
        run = self.make_run(("count_partners", {"domain": []}))
        for vals in ({"state": "draft"}, {"plan": "[]"}, {"agent_id": self.agent.id}):
            with self.assertRaises(AccessError):
                run.with_context(internal=True).write(vals)
        with self.assertRaises(UserError):
            run.action_start()

    def test_call_and_audit_are_protected(self):
        run = self.make_run(("count_partners", {"domain": []}))
        run._advance()
        for record in (run.call_ids, run.step_ids):
            with self.assertRaises(AccessError):
                record.with_context(internal=True).write({"args": "{}"})
            with self.assertRaises(AccessError):
                record.unlink()
            with self.assertRaises(AccessError):
                record.create({})

    def test_pending_grant_revocation_is_audited_and_never_executes(self):
        run = self.make_run(("create_partner", {"vals_list": [{"name": "Revoked"}]}))
        run._advance()
        run.action_approve()
        self.agent.tool_ids -= self.tool_create
        run.action_run_to_completion()
        self.assertEqual(run.state, "failed")
        self.assertEqual(run.call_ids.state, "refused")
        self.assertFalse(self.env["res.partner"].search([("name", "=", "Revoked")]))

    def test_pending_policy_change_refuses_dispatch(self):
        run = self.make_run(("create_partner", {"vals_list": [{"name": "Changed"}]}))
        run._advance()
        run.action_approve()
        self.tool_create.requires_approval = False
        run._advance()
        self.assertEqual(run.call_ids.state, "refused")

    def test_forged_default_context_cannot_create_running_run(self):
        with self.assertRaises(AccessError):
            self.Run.with_context(default_state="running").create(
                {"agent_id": self.agent.id}
            )

    def test_default_context_does_not_forge_invocation_or_evidence(self):
        run = self.make_run(("create_partner", {"vals_list": [{"name": "Defaults"}]}))
        run = run.with_context(
            default_approver_id=self.agent_user.id, default_receipt={"fake": True}
        )
        run._advance()
        self.assertEqual(run.state, "awaiting_approval")
        self.assertFalse(run.call_ids.approver_id)
        self.assertFalse(run.call_ids.receipt)
        run.with_context(default_judged_at="2000-01-01").action_approve()
        self.assertGreater(run.call_ids.evidence_ids.judged_at.year, 2000)

    def test_static_tool_retargeting_invalidates_pending_snapshot(self):
        run = self.make_run(("create_partner", {"vals_list": [{"name": "Retargeted"}]}))
        run._advance()
        run.action_approve()
        self.tool_create.method_name = "name_search"
        run._advance()
        self.assertEqual(run.call_ids.state, "refused")
