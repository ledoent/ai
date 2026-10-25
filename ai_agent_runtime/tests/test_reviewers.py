from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from .common import AgentRuntimeCommon


@tagged("post_install", "-at_install")
class TestReviewers(AgentRuntimeCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.reviewer = new_test_user(
            cls.env,
            login="assigned_reviewer",
            groups="base.group_user,ai_agent_runtime.group_ai_reviewer",
        )
        cls.outsider = new_test_user(
            cls.env,
            login="unassigned_reviewer",
            groups="base.group_user,ai_agent_runtime.group_ai_reviewer",
        )
        cls.agent.reviewer_ids += cls.reviewer

    def observed(self):
        self.agent._capability(self.tool_create).action_set_level(
            "observe", "Observe review"
        )
        run = self.make_run(
            ("create_partner", {"vals_list": [{"name": "Intent only"}]})
        )
        run._advance()
        return run.call_ids

    def test_accepting_observed_intent_does_not_execute(self):
        call = self.observed()
        call.with_user(self.reviewer).action_accept("Good intent")
        self.assertEqual(call.state, "observed")
        self.assertEqual(call.evidence_ids.verdict, "accept")
        self.assertEqual(call.evidence_ids.reviewer_id, self.reviewer)
        self.assertFalse(self.env["res.partner"].search([("name", "=", "Intent only")]))

    def test_judgment_cannot_be_repeated(self):
        call = self.observed()
        call.with_user(self.reviewer).action_accept()
        with self.assertRaises(UserError):
            call.with_user(self.reviewer).action_reject()
        self.assertEqual(len(call.evidence_ids), 1)

    def test_group_and_assignment_are_both_required(self):
        call = self.observed()
        with self.assertRaises(AccessError):
            call.with_user(self.outsider).action_accept()
        self.outsider.group_ids -= self.env.ref("ai_agent_runtime.group_ai_reviewer")
        self.agent.reviewer_ids += self.outsider
        with self.assertRaises(AccessError):
            call.with_user(self.outsider).action_accept()

    def test_other_agent_execution_account_cannot_review(self):
        call = self.observed()
        self.env["ai.agent"].create(
            {"name": "Other agent", "user_id": self.reviewer.id}
        )
        with self.assertRaises(AccessError):
            call.with_user(self.reviewer).action_accept()

    def test_own_execution_account_cannot_review_even_with_group(self):
        call = self.observed()
        self.agent_user.group_ids += self.env.ref("ai_agent_runtime.group_ai_reviewer")
        self.agent.reviewer_ids += self.agent_user
        with self.assertRaises(AccessError):
            call.with_user(self.agent_user).with_context(internal=True).action_accept()

    def test_evidence_cannot_be_manufactured_or_edited(self):
        call = self.observed()
        Evidence = self.env["ai.agent.evidence"]
        with self.assertRaises(AccessError):
            Evidence.with_context(internal=True).create({"call_id": call.id})
        call.action_accept()
        with self.assertRaises(AccessError):
            call.evidence_ids.write({"verdict": "reject"})
        with self.assertRaises(AccessError):
            call.evidence_ids.unlink()

    def test_run_call_and_evidence_visibility_is_scoped(self):
        call = self.observed()
        call.action_accept()
        run = call.run_id
        for record in (run, call, call.evidence_ids, run.step_ids):
            self.assertFalse(
                record.with_user(self.outsider).search([("id", "in", record.ids)])
            )
            with self.assertRaises(AccessError):
                record.with_user(self.outsider).read(["id"])
            self.assertEqual(
                record.with_user(self.reviewer).read(["id"])[0]["id"], record.id
            )
            self.assertEqual(
                record.with_user(self.agent_user).read(["id"])[0]["id"], record.id
            )

    def test_database_constraint_prevents_duplicate_judgment(self):
        from psycopg2.errors import UniqueViolation

        from odoo.tools import mute_logger

        call = self.observed()
        call.action_accept()
        with (
            self.assertRaises(UniqueViolation),
            mute_logger("odoo.sql_db"),
            self.cr.savepoint(),
        ):
            self.env["ai.agent.evidence"]._record(
                call, "judgment", "accept", "Duplicate"
            )

    def test_business_judgment_overrides_invocation_judgment_and_deduplicates(self):
        run = self.make_run(("count_partners", {"domain": []}))
        run._advance()
        first = run.call_ids
        Evidence = self.env["ai.agent.evidence"]
        Evidence._record(first, "judgment", "accept", "Invocation accepted")
        Evidence.with_user(self.reviewer)._record_business(
            first, "reject", "The selected document was wrong", "business-intent-1"
        )
        self.assertEqual(first.capability_id._statistics()["approvals"], 0)
        self.assertEqual(first.capability_id._statistics()["rejections"], 1)

        second_run = self.make_run(("count_partners", {"domain": []}))
        second_run._advance()
        second = second_run.call_ids
        duplicate = Evidence.with_user(self.reviewer)._record_business(
            second, "accept", "Repeated intent", "business-intent-1"
        )
        self.assertFalse(duplicate)
        self.assertEqual(first.capability_id._statistics()["rejections"], 1)

    def test_reason_wizard_cannot_bypass_reviewer_or_admin_authority(self):
        cap = self.agent._capability(self.tool_create)
        wizard = (
            self.env["ai.agent.authority.wizard"]
            .with_user(self.reviewer)
            .create(
                {
                    "capability_id": cap.id,
                    "operation": "manual",
                    "level": "apply",
                    "reason": "Forged",
                }
            )
        )
        with self.assertRaises(AccessError):
            wizard.action_confirm()
        wizard.operation = "veto"
        wizard.action_confirm()
        self.assertEqual(cap.level, "observe")
        self.assertTrue(cap.promotion_locked)
