from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from .common import AgentRuntimeCommon


@tagged("post_install", "-at_install")
class TestPromotion(AgentRuntimeCommon):
    def observe(self, accept=True):
        run = self.make_run(("create_partner", {"vals_list": [{"name": "Evidence"}]}))
        run._advance()
        call = run.call_ids
        if accept:
            call.action_accept()
        else:
            call.action_reject()
        return call

    def observe_cap(self):
        cap = self.agent._capability(self.tool_create)
        cap.action_set_level("observe", "Evidence fixture")
        return cap

    def test_twenty_fresh_samples_purchase_only_one_promotion(self):
        cap = self.observe_cap()
        for _index in range(19):
            self.observe()
        self.assertFalse(cap._evaluate_promotion())
        self.observe()
        self.env["ai.agent.capability"]._cron_promote()
        self.assertEqual(cap.level, "propose")
        self.assertEqual(cap._statistics()["approvals"], 0)
        self.assertFalse(cap._evaluate_promotion())
        event = cap.history[-1]
        self.assertEqual(event["approvals"], 20)
        self.assertEqual(event["previous_level"], "observe")
        self.assertEqual(event["new_level"], "propose")
        self.assertTrue(event["period_start"])
        self.assertTrue(event["period_end"])

    def test_rate_requires_configured_agreement(self):
        cap = self.observe_cap()
        for _index in range(3):
            self.observe(False)
            self.observe()
        for _index in range(14):
            self.observe()
        self.assertEqual(cap._statistics()["approvals"], 17)
        self.assertFalse(cap._evaluate_promotion())
        for _index in range(10):
            self.observe()
        self.assertTrue(cap._evaluate_promotion())

    def test_stale_observation_judgments_cannot_promote(self):
        cap = self.observe_cap()
        run = self.make_run(("create_partner", {"vals_list": [{"name": "Old epoch"}]}))
        run._advance()
        call = run.call_ids
        cap.action_set_level("observe", "New epoch")
        call.action_accept()
        self.assertEqual(cap._statistics()["approvals"], 0)
        self.assertEqual(call.evidence_ids.epoch, call.capability_epoch)

    def test_pending_approval_and_execution_failure_provide_no_positive(self):
        run = self.make_run(("create_partner", {"invalid": "bad signature"}))
        run._advance()
        run.action_approve()
        cap = self.agent._capability(self.tool_create)
        self.assertEqual(cap._statistics()["approvals"], 0)
        run._advance()
        self.assertEqual(run.call_ids.state, "failed")
        self.assertEqual(cap._statistics()["approvals"], 0)

    def test_unreviewed_success_provides_no_positive(self):
        cap = self.agent._capability(self.tool_count)
        for _index in range(20):
            self.make_run(("count_partners", {"domain": []}))._advance()
        self.assertEqual(cap._statistics()["approvals"], 0)
        self.assertFalse(cap._evaluate_promotion())

    def test_three_rejections_demote_and_reset_epoch(self):
        cap = self.agent._capability(self.tool_create)
        epoch = cap.epoch
        for _index in range(3):
            run = self.make_run(
                ("create_partner", {"vals_list": [{"name": "Rejected"}]})
            )
            run._advance()
            run.action_reject()
        self.assertEqual(cap.level, "observe")
        self.assertGreater(cap.epoch, epoch)
        self.assertEqual(cap.history[-1]["rejections"], 3)
        self.assertEqual(cap._statistics()["rejections"], 0)

    def test_writer_without_adapter_stops_at_propose(self):
        cap = self.agent._capability(self.tool_create)
        for _index in range(20):
            run = self.make_run(
                ("create_partner", {"vals_list": [{"name": "Reviewed"}]})
            )
            run._advance()
            run.action_approve()
            run._advance()
        self.assertEqual(cap._statistics()["approvals"], 20)
        self.assertFalse(cap._evaluate_promotion())
        self.assertEqual(cap.level, "propose")

    def test_veto_is_persistent_until_administrator_clears_it(self):
        cap = self.observe_cap()
        reviewer = new_test_user(
            self.env,
            login="veto_reviewer",
            groups="base.group_user,ai_agent_runtime.group_ai_reviewer",
        )
        self.agent.reviewer_ids += reviewer
        cap.with_user(reviewer).action_veto("Stop until investigated")
        for _index in range(20):
            self.observe()
        self.assertFalse(cap._evaluate_promotion())
        self.assertTrue(cap.promotion_locked)
        with self.assertRaises(AccessError):
            cap.with_user(reviewer).action_clear_lock("Tried")
        cap.action_clear_lock("Investigation complete")
        self.assertFalse(cap.promotion_locked)
        self.assertEqual(cap._statistics()["approvals"], 0)

    def test_manual_level_requires_reason(self):
        cap = self.observe_cap()
        with self.assertRaises(UserError):
            cap.action_set_level("apply", " ")
        cap.action_set_level("apply", "Explicit administrative override")
        self.assertEqual(cap.level, "apply")

    def test_undo_without_adapter_removes_apply_before_reporting_failure(self):
        cap = self.agent._capability(self.tool_create)
        self.tool_create.requires_approval = False
        cap.action_set_level("apply", "Manual fixture")
        run = self.make_run(("create_partner", {"vals_list": [{"name": "Repair me"}]}))
        run._advance()
        call = run.call_ids
        call.action_undo("Wrong action")
        self.assertEqual(cap.level, "propose")
        self.assertEqual(call.state, "dispatched")
        self.assertEqual(call.undo_state, "failed")
        self.assertIn("manual repair", call.undo_error)
        with self.assertRaises(UserError):
            call.action_undo("Again")
        self.assertEqual(len(call.evidence_ids.filtered(lambda e: e.kind == "undo")), 1)

    def test_three_rejections_remove_apply_authority(self):
        cap = self.agent._capability(self.tool_create)
        cap.action_set_level("apply", "Review-gated apply fixture")
        for _index in range(3):
            run = self.make_run(
                ("create_partner", {"vals_list": [{"name": "Negative apply"}]})
            )
            run._advance()
            run.action_reject()
        self.assertEqual(cap.level, "propose")
        self.assertEqual(cap._statistics()["rejections"], 0)
