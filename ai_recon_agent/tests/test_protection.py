from odoo.exceptions import AccessError
from odoo.tests import Command, TransactionCase, new_test_user, tagged


@tagged("post_install", "-at_install")
class TestReconciliationProtection(TransactionCase):
    def test_assessment_cannot_be_created_through_orm(self):
        with self.assertRaises(AccessError):
            self.env["ai.recon.assessment"].create({})

    def test_match_cannot_be_created_through_orm(self):
        with self.assertRaises(AccessError):
            self.env["ai.recon.match"].create({})

    def test_allocation_cannot_be_created_through_orm(self):
        with self.assertRaises(AccessError):
            self.env["ai.recon.match.allocation"].create({})

    def test_service_rejects_direct_calls_and_forged_context(self):
        service = self.env["ai.recon.match.service"].with_context(
            ai_agent_invocation={"tool": "ai.recon.match.service.match_items"}
        )
        with self.assertRaises(AccessError):
            service.match_items(1, [1])

    def test_company_reconciliation_settings_require_a_human_admin(self):
        user = new_test_user(
            self.env, login="recon_profile_actor", groups="base.group_user"
        )
        company = self.env.company
        with self.assertRaises(AccessError):
            company.with_user(user).write({"ai_recon_agent_enabled": True})

        self.env["ai.agent"].create(
            {"name": "Reconciliation config identity fixture", "user_id": user.id}
        )
        user.write({"group_ids": [Command.link(self.env.ref("base.group_system").id)]})
        with self.assertRaises(AccessError):
            company.with_user(user).write({"ai_recon_agent_enabled": True})
