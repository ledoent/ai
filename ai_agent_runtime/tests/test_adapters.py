from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import new_test_user, tagged

from .adapter_fixture import SetValueAdapter, register_fixture, unregister_fixture
from .common import AgentRuntimeCommon


@tagged("post_install", "-at_install")
class TestAdapters(AgentRuntimeCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.transaction.will_change_registry()
        register_fixture(cls.env)
        cls.addClassCleanup(unregister_fixture, cls.env)
        cls.subject = cls.env["ai.runtime.contract.item"].create({})
        cls.tool = cls.Tool.create(
            {
                "name": "set_value",
                "description": "Change the contract subject value.",
                "model_name": cls.subject._name,
                "method_name": "action_set_value",
            }
        )
        cls.agent.tool_ids += cls.tool
        cls.cap = cls.agent._capability(cls.tool)

    def run_value(self, value=42, **args):
        return self.make_run(
            ("set_value", {"item_id": self.subject.id, "value": value, **args})
        )

    def samples(self, n=20):
        for index in range(n):
            run = self.run_value(index)
            run._advance()
            if run.state == "awaiting_approval":
                run.action_approve()
                run._advance()
            else:
                run.call_ids.action_accept()

    def test_complete_promotion_and_undo_path(self):
        original = self.subject.value
        self.samples()
        self.assertEqual(self.subject.value, original)
        self.assertTrue(self.cap._evaluate_promotion())
        self.assertEqual(self.cap.level, "propose")
        self.samples()
        self.assertTrue(self.cap._evaluate_promotion())
        self.assertEqual(self.cap.level, "apply")
        before = self.subject.value
        run = self.run_value(777)
        run._advance()
        call = run.call_ids
        self.assertEqual(self.subject.value, 777)
        self.assertEqual(self.subject.write_uid, self.agent_user)
        call.action_undo("Restore the previous value")
        self.assertEqual(self.subject.value, before)
        self.assertEqual(call.state, "undone")
        self.assertEqual(call.undo_state, "done")
        self.assertEqual(self.cap.level, "propose")
        self.assertEqual(self.cap._statistics()["approvals"], 0)
        self.assertFalse(self.cap._evaluate_promotion())

    def test_observe_preserves_value_write_date_and_never_dispatches(self):
        self.subject.flush_recordset()
        before = (self.subject.value, self.subject.write_date, self.subject.revision)
        with (
            patch.object(SetValueAdapter, "prepare") as prepare,
            patch("requests.post") as http,
            patch.object(type(self.env["mail.mail"]), "send") as mail,
        ):
            run = self.run_value()
            run._advance()
        self.subject.invalidate_recordset()
        self.assertEqual(
            (self.subject.value, self.subject.write_date, self.subject.revision), before
        )
        prepare.assert_not_called()
        http.assert_not_called()
        mail.assert_not_called()
        self.assertEqual(run.call_ids.state, "observed")

    def test_propose_executes_the_stored_arguments(self):
        self.cap.action_set_level("propose", "Snapshot fixture")
        run = self.run_value(99)
        run._advance()
        snapshot = run.pending_args
        run.action_approve()
        run._advance()
        self.assertEqual(self.subject.value, 99)
        self.assertEqual(run.step_ids.args, snapshot)
        self.assertEqual(self.cap._statistics()["approvals"], 1)

    def test_execute_failure_is_atomic(self):
        self.cap.action_set_level("apply", "Atomic fixture")
        before = (self.subject.value, self.subject.revision)
        run = self.run_value(999, fail=True)
        run._advance()
        self.subject.invalidate_recordset()
        self.assertEqual((self.subject.value, self.subject.revision), before)
        self.assertEqual(run.call_ids.state, "failed")
        self.assertFalse(run.call_ids.receipt)

    def test_receipt_failure_rolls_back_execution(self):
        self.cap.action_set_level("apply", "Capture failure fixture")
        before = self.subject.value
        with patch.object(
            SetValueAdapter, "capture", side_effect=ValueError("Receipt failed")
        ):
            run = self.run_value(999)
            run._advance()
        self.assertEqual(self.subject.value, before)
        self.assertEqual(run.call_ids.state, "failed")

    def test_intervening_change_prevents_undo_and_preserves_downgrade(self):
        self.cap.action_set_level("apply", "Intervention fixture")
        run = self.run_value(88)
        run._advance()
        self.subject.value = 123
        run.call_ids.action_undo("Reverse this")
        self.assertEqual(self.subject.value, 123)
        self.assertEqual(self.cap.level, "propose")
        self.assertEqual(run.call_ids.state, "dispatched")
        self.assertEqual(run.call_ids.undo_state, "failed")
        self.assertIn("Intervening changes", run.call_ids.undo_error)

    def test_aba_change_is_detected_by_revision(self):
        self.cap.action_set_level("apply", "ABA fixture")
        run = self.run_value(88)
        run._advance()
        self.subject.value = 123
        self.subject.value = 88
        run.call_ids.action_undo("ABA must not pass")
        self.assertEqual(run.call_ids.undo_state, "failed")
        self.assertEqual(self.subject.value, 88)

    def test_reversal_failure_is_atomic_but_demotion_survives(self):
        self.cap.action_set_level("apply", "Failed reversal fixture")
        run = self.run_value(88)
        run._advance()

        def fail_after_write(adapter, record, receipt):
            self.assertEqual(self.cap.level, "propose")
            record.browse(receipt["item_id"]).value = -99
            raise UserError(record.env._("Reversal failed after writing"))

        with patch.object(SetValueAdapter, "reverse", fail_after_write):
            run.call_ids.action_undo("Try reversal")
        self.assertEqual(self.subject.value, 88)
        self.assertEqual(self.cap.level, "propose")
        self.assertEqual(run.call_ids.undo_state, "failed")

    def test_reversal_contract_change_invalidates_pending_call(self):
        self.cap.action_set_level("propose", "Contract fixture")
        run = self.run_value()
        run._advance()
        run.action_approve()
        with patch.object(SetValueAdapter, "contract_id", "set-value-v2"):
            run._advance()
        self.assertEqual(run.call_ids.state, "refused")
        self.assertEqual(self.subject.value, 7)
        self.assertEqual(self.cap.level, "observe")

    def test_irreversible_guard_blocks_apply_even_with_adapter(self):
        self.agent.promotion_samples = 1
        tool = self.Tool.create(
            {
                "name": "confirm_value",
                "description": "Confirm a contract value.",
                "model_name": self.subject._name,
                "method_name": "action_confirm",
            }
        )
        self.agent.tool_ids += tool
        cap = self.agent._capability(tool)
        self.assertEqual(tool.classification, "irreversible")
        self.assertTrue(tool._undo_adapter())
        cap.action_set_level("propose", "Guard fixture")
        run = self.make_run((tool.name, {"item_id": self.subject.id, "value": 8}))
        run._advance()
        run.action_approve()
        run._advance()
        self.assertFalse(cap._evaluate_promotion())
        cap.action_set_level("apply", "Human override of the irreversible guard")
        self.assertEqual(cap.level, "apply")

    def test_readonly_tools_need_no_reversal_adapter(self):
        self.agent.promotion_samples = 1
        self.tool_count.requires_approval = True
        cap = self.agent._capability(self.tool_count)
        for expected in ("propose", "apply"):
            run = self.make_run(("count_partners", {"domain": []}))
            run._advance()
            run.action_approve()
            run._advance()
            self.assertTrue(cap._evaluate_promotion())
            self.assertEqual(cap.level, expected)

    def test_promotion_notifies_assigned_reviewers(self):
        reviewer = new_test_user(
            self.env,
            login="promotion_notify",
            notification_type="inbox",
            groups="base.group_user,ai_agent_runtime.group_ai_reviewer",
        )
        self.agent.reviewer_ids += reviewer
        self.agent.promotion_samples = 1
        self.run_value()._advance()
        call = self.agent.run_ids[:1].call_ids
        call.action_accept()
        self.cap._evaluate_promotion()
        notification = self.env["mail.notification"].search(
            [
                ("res_partner_id", "=", reviewer.partner_id.id),
                ("mail_message_id.model", "=", self.cap._name),
                ("mail_message_id.res_id", "=", self.cap.id),
            ]
        )
        self.assertTrue(notification)

    def test_uninstalled_target_is_an_authorization_refusal(self):
        self.cap.action_set_level("propose", "Uninstall fixture")
        run = self.run_value()
        run._advance()
        run.action_approve()
        definition = self.registry.models.pop(self.subject._name)
        try:
            run._advance()
        finally:
            self.registry.models[self.subject._name] = definition
        self.assertEqual(run.call_ids.state, "refused")
        self.assertEqual(run.state, "failed")
