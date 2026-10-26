import json
from datetime import date
from typing import ClassVar
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.ai_agent_runtime.tests.common import AgentRuntimeCommon


@tagged("post_install", "-at_install")
class TestAiAgentUsage(AgentRuntimeCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env["ai.agent.provider"].create(
            {"name": "Usage stub", "kind": "stub", "model": "stub-model"}
        )
        cls.agent.provider_id = cls.provider

    def make_goal_run(self, script):
        run = self.Run.create(
            {"agent_id": self.agent.id, "goal": "Count records", "plan": "[]"}
        )
        run.action_start()
        return run.with_context(stub_script=script)

    def test_unbudgeted_call_records_unpriced_usage_without_prompt(self):
        run = self.make_goal_run([{"kind": "final", "answer": "done"}])
        run.action_run_to_completion()
        attempt = self.env["ai.agent.usage.attempt"].search(
            [("run_db_id", "=", run.id)]
        )
        self.assertEqual(len(attempt), 1)
        self.assertEqual(attempt.state, "settled")
        self.assertEqual(attempt.usage_status, "unavailable")
        self.assertEqual(attempt.cost_status, "unpriced")
        self.assertEqual(attempt.reservation_state, "released")
        self.assertFalse(attempt.budget_enforced)
        self.assertEqual(json.loads(attempt.decision_json)["answer"], "done")
        self.assertNotIn("goal", attempt._fields)
        self.assertNotIn("prompt", attempt._fields)

    def test_reported_provider_cost_is_preferred_and_keeps_its_currency(self):
        company = self.agent_user.company_id
        run = self.make_goal_run(
            [
                {
                    "kind": "final",
                    "answer": "done",
                    "cost": {"amount": 1.25, "currency": company.currency_id.name},
                }
            ]
        )
        run.action_run_to_completion()
        attempt = self.env["ai.agent.usage.attempt"].search(
            [("run_db_id", "=", run.id)]
        )
        self.assertEqual(attempt.usage_status, "unavailable")
        self.assertEqual(attempt.cost_status, "reported")
        self.assertEqual(attempt.cost_provenance, "provider")
        self.assertEqual(attempt.provider_cost_currency_id, company.currency_id)
        self.assertEqual(attempt.provider_cost_amount, 1.25)
        self.assertEqual(attempt.charged_amount, 1.25)

    def test_rate_cards_are_immutable_and_versioned(self):
        Rate = self.env["ai.agent.usage.rate"]
        values = {
            "provider_kind": "stub",
            "model": "stub-model",
            "input_per_million": 2.0,
            "output_per_million": 4.0,
            "currency_id": self.env.company.currency_id.id,
            "request_ceiling": 10.0,
        }
        first = Rate.create(values)
        second = Rate.create(dict(values, input_per_million=3.0))
        self.assertEqual((first.version, second.version), (1, 2))
        with self.assertRaises(AccessError):
            first.write({"input_per_million": 99.0})

    def test_missing_reported_usage_uses_the_selected_estimator(self):
        Attempt = self.env["ai.agent.usage.attempt"]
        with (
            patch.object(
                type(Attempt),
                "_estimate_input",
                return_value=(12, "anthropic_count_tokens"),
            ),
            patch.object(
                type(Attempt),
                "_estimate_output",
                return_value=(7, "local_tiktoken_approximation"),
            ),
        ):
            tokens = Attempt._normalize_usage(
                self.provider,
                {"decision": {"kind": "final"}, "usage": {}},
                "prompt is passed to the estimator, not saved",
                [],
                [],
            )
        self.assertEqual(
            tokens,
            (12, 7, "estimated", "mixed_estimator"),
        )

    def test_openai_provider_exposes_normalized_usage_and_request_id(self):
        import os

        provider = self.env["ai.agent.provider"].create(
            {
                "name": "Mock OpenAI",
                "kind": "openai",
                "model": "gpt-4o-mini",
                "api_key_env": "OPENAI_USAGE_TEST_KEY",
            }
        )

        class Response:
            status_code = 200
            headers: ClassVar[dict[str, str]] = {"x-request-id": "openai-request-1"}

            @staticmethod
            def json():
                return {
                    "id": "chatcmpl-1",
                    "model": "gpt-4o-mini-2025-01-01",
                    "choices": [{"message": {"content": "done"}}],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 2},
                }

        with (
            patch.dict(os.environ, {"OPENAI_USAGE_TEST_KEY": "test-only"}),
            patch(
                "odoo.addons.ai_agent_llm.models.ai_agent_provider.requests.post",
                return_value=Response(),
            ),
        ):
            result = provider._decide_result("goal", [], [])
        self.assertEqual(result["decision"], {"kind": "final", "answer": "done"})
        self.assertEqual(result["request_id"], "openai-request-1")
        self.assertEqual(result["usage"], {"input_tokens": 9, "output_tokens": 2})
        self.assertEqual(result["usage_status"], "reported")
        self.assertIsNone(result["cost"])

    def test_tracking_does_not_backfill_runs_without_provider_attempts(self):
        company = self.agent_user.company_id
        run = self.Run.create({"agent_id": self.agent.id, "plan": "[]"})
        run.action_start()
        run.action_run_to_completion()
        self.assertFalse(
            self.env["ai.agent.usage.attempt"].search([("run_db_id", "=", run.id)])
        )
        self.assertFalse(company.ai_usage_tracking_started_at)

    def test_reserved_but_unsent_attempt_is_reused(self):
        run = self.make_goal_run([{"kind": "final", "answer": "done"}])
        Attempt = self.env["ai.agent.usage.attempt"]
        period = Attempt._company_month(run.billing_company_id)
        reserved = Attempt._reserve(run, self.provider, False, period)
        self.assertEqual(reserved.state, "reserved")
        run.action_run_to_completion()
        rows = Attempt.search([("run_db_id", "=", run.id)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.state, "settled")

    def test_run_provider_and_model_are_frozen_at_start(self):
        run = self.make_goal_run([{"kind": "final", "answer": "old model"}])
        self.provider.model = "changed-after-start"
        run.action_run_to_completion()
        attempt = self.env["ai.agent.usage.attempt"].search(
            [("run_db_id", "=", run.id)]
        )
        self.assertEqual(run.usage_model_snapshot, "stub-model")
        self.assertEqual(attempt.model, "stub-model")
        self.assertEqual(json.loads(attempt.decision_json)["answer"], "old model")

    def test_monthly_and_run_reservations_preserve_shared_headroom(self):
        company = self.agent_user.company_id
        company.ai_usage_enabled = True
        company.ai_usage_monthly_limit = 5.0
        rate = self.env["ai.agent.usage.rate"].create(
            {
                "provider_kind": "stub",
                "model": "stub-model",
                "input_per_million": 1.0,
                "output_per_million": 1.0,
                "currency_id": company.currency_id.id,
                "request_ceiling": 5.0,
            }
        )
        first = self.make_goal_run([])
        second = self.make_goal_run([])
        Attempt = self.env["ai.agent.usage.attempt"]
        first_attempt = Attempt._reserve(first, self.provider, rate, date(2026, 10, 1))
        second_attempt = Attempt._reserve(
            second, self.provider, rate, date(2026, 10, 1)
        )
        self.assertEqual(first_attempt.state, "reserved")
        self.assertTrue(first_attempt.budget_enforced)
        self.assertEqual(second_attempt.state, "blocked")

        company.ai_usage_monthly_limit = 100.0
        budget = self.env["ai.agent.usage.budget"].create(
            {
                "company_id": company.id,
                "agent_id": self.agent.id,
                "monthly_limit": 5.0,
                "run_limit": 0.0,
            }
        )
        third = self.make_goal_run([])
        fourth = self.make_goal_run([])
        self.assertEqual(
            Attempt._reserve(third, self.provider, rate, date(2026, 11, 1)).state,
            "reserved",
        )
        self.assertEqual(
            Attempt._reserve(fourth, self.provider, rate, date(2026, 11, 1)).state,
            "blocked",
        )

        budget.monthly_limit = 0.0
        budget.run_limit = 5.0
        fifth = self.make_goal_run([])
        run_one = Attempt._reserve(fifth, self.provider, rate, date(2026, 12, 1))
        run_two = Attempt._reserve(fifth, self.provider, rate, date(2027, 1, 1))
        self.assertEqual(run_one.state, "reserved")
        self.assertEqual(run_two.state, "blocked")

    def test_uncertain_send_pauses_and_cannot_resume_until_resolved(self):
        company = self.agent_user.company_id
        company.ai_usage_enabled = True
        rate = self.env["ai.agent.usage.rate"].create(
            {
                "provider_kind": "stub",
                "model": "stub-model",
                "input_per_million": 1.0,
                "output_per_million": 1.0,
                "currency_id": company.currency_id.id,
                "request_ceiling": 5.0,
            }
        )
        run = self.make_goal_run([{"kind": "final", "answer": "must not resend"}])
        Attempt = self.env["ai.agent.usage.attempt"]
        row = Attempt._reserve(
            run,
            run.provider_snapshot_id,
            rate,
            Attempt._company_month(run.billing_company_id),
        )
        row._set(
            {
                "state": "sending",
                "send_state": "sending",
                "reservation_state": "held",
            }
        )
        self.assertTrue(run._before_provider_decision())
        self.assertEqual(run.state, "usage_hold")
        with self.assertRaises(UserError):
            run.action_resume_usage()
        row.resolution_amount_input = 2.0
        row.resolution_note_input = "Reviewed provider trace; charge accepted."
        row.action_adjust_hold()
        self.assertEqual(row.state, "adjusted")
        self.assertEqual(row.resolution_action, "adjust")
        self.assertEqual(
            row.resolution_note, "Reviewed provider trace; charge accepted."
        )
        self.assertEqual(row.resolved_by, self.env.user)
        self.assertTrue(row.resolved_at)
        run.action_resume_usage()
        self.assertEqual(run.state, "running")
        self.assertEqual(run.step_no, 0)

    def test_overage_dispatches_returned_tool_then_holds_before_next_request(self):
        company = self.agent_user.company_id
        company.ai_usage_enabled = True
        company.ai_usage_monthly_limit = 100.0
        self.env["ai.agent.usage.rate"].create(
            {
                "provider_kind": "stub",
                "model": "stub-model",
                "input_per_million": 1.0,
                "output_per_million": 1.0,
                "currency_id": company.currency_id.id,
                "request_ceiling": 0.01,
            }
        )
        run = self.make_goal_run(
            [
                {
                    "kind": "tool",
                    "tool": self.tool_create.name,
                    "args": {"vals_list": [{"name": "overage approved tool"}]},
                    "cost": {"amount": 1.25, "currency": company.currency_id.name},
                },
                {"kind": "final", "answer": "this call must be paused"},
            ]
        )
        run.action_run_to_completion()
        attempt = self.env["ai.agent.usage.attempt"].search(
            [("run_db_id", "=", run.id)]
        )
        self.assertEqual(run.state, "awaiting_approval")
        self.assertEqual(run.step_no, 0)
        self.assertEqual(run.pending_call_id.state, "pending")
        self.assertEqual(len(attempt), 1)
        self.assertEqual(attempt.state, "overage")
        self.assertEqual(attempt.charged_amount, 1.25)
        run.action_approve()
        run.action_run_to_completion()
        self.assertEqual(run.state, "usage_hold")
        self.assertEqual(run.step_no, 1)
        self.assertEqual(run.call_ids.state, "dispatched", run.call_ids.result)
        self.assertTrue(
            self.env["res.partner"].search_count(
                [("name", "=", "overage approved tool")]
            )
        )

    def test_multi_company_run_requires_one_selected_billing_company(self):
        first = self.agent_user.company_id
        second = self.env["res.company"].create(
            {"name": "Usage Billing Company", "currency_id": first.currency_id.id}
        )
        self.agent_user.company_ids = [(4, second.id)]
        run = self.Run.create(
            {
                "agent_id": self.agent.id,
                "goal": "No provider call",
                "plan": "[]",
                "company_ids": [(6, 0, [first.id, second.id])],
            }
        )
        with self.assertRaises(UserError):
            run.action_start()
        run.billing_company_id = second
        run.action_start()
        self.assertEqual(run.billing_company_id, second)

    def test_missing_rate_blocks_budgeted_request_then_admin_resolves_and_resumes(self):
        company = self.agent_user.company_id
        company.ai_usage_enabled = True
        company.ai_usage_monthly_limit = 100.0
        run = self.make_goal_run([{"kind": "final", "answer": "known response"}])
        run.action_run_to_completion()
        self.assertEqual(run.state, "usage_hold")
        blocked = self.env["ai.agent.usage.attempt"].search(
            [("run_db_id", "=", run.id)], order="id desc", limit=1
        )
        self.assertEqual(blocked.state, "blocked")
        self.assertEqual(blocked.send_state, "not_sent")

        self.env["ai.agent.usage.rate"].create(
            {
                "provider_kind": "stub",
                "model": "stub-model",
                "input_per_million": 1.0,
                "output_per_million": 1.0,
                "currency_id": company.currency_id.id,
                "request_ceiling": 5.0,
            }
        )
        run.action_resume_usage()
        run.action_run_to_completion()
        uncertain = self.env["ai.agent.usage.attempt"].search(
            [("run_db_id", "=", run.id), ("state", "=", "unknown")],
            order="id desc",
            limit=1,
        )
        self.assertTrue(uncertain)
        self.assertEqual(uncertain.reservation_state, "held")
        self.assertEqual(run.state, "usage_hold")
        with self.assertRaises(AccessError):
            uncertain.with_user(self.agent_user).action_release_hold()
        uncertain.resolution_note_input = (
            "Provider response was captured by the test stub."
        )
        uncertain.action_release_hold()
        run.action_resume_usage()
        run.action_run_to_completion()
        self.assertEqual(run.state, "done")
        self.assertEqual(run.answer, "known response")

    def test_estimated_cost_and_duplicate_settlement_are_idempotent(self):
        company = self.agent_user.company_id
        Rate = self.env["ai.agent.usage.rate"]
        rate = Rate.create(
            {
                "provider_kind": "stub",
                "model": "stub-model",
                "input_per_million": 1_000_000.0,
                "output_per_million": 1_000_000.0,
                "currency_id": self.env.ref("base.EUR").id,
                "request_ceiling": 4.0,
            }
        )
        run = self.make_goal_run([{"kind": "final", "answer": "priced"}])
        Attempt = self.env["ai.agent.usage.attempt"]
        row = Attempt._new(
            {
                "attempt_uuid": "fixed-settlement-test",
                "run_db_id": run.id,
                "run_name": run.display_name,
                "sequence": 0,
                "ordinal": 88,
                "agent_id": self.agent.id,
                "company_id": company.id,
                "provider_kind": "stub",
                "provider_name": self.provider.name,
                "model": "stub-model",
                "state": "sending",
                "budget_enforced": True,
                "send_state": "sending",
                "reservation_state": "held",
                "usage_status": "unavailable",
                "usage_provenance": "unavailable",
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_status": "unpriced",
                "cost_provenance": "unpriced",
                "rate_id": rate.id,
                "rate_version": rate.version,
                "rate_currency_id": self.env.ref("base.EUR").id,
                "company_currency_id": company.currency_id.id,
                "fx_rate": 1.25,
                "fx_date": "2026-10-25",
                "request_ceiling": 4.0,
                "reservation_amount": 4.0,
                "charged_amount": 0.0,
                "budget_period_start": "2026-10-01",
                "request_at": "2026-10-25 12:00:00",
            }
        )
        result = {
            "decision": {"kind": "final", "answer": "priced"},
            "provider_kind": "stub",
            "model": "stub-model",
            "request_id": "req-123",
            "usage": {"input_tokens": 2, "output_tokens": 3},
            "usage_status": "reported",
        }
        service = Attempt
        settled, status = service._settle(
            row, result.copy(), self.provider, "not persisted", [], [], rate
        )
        settled_again, status_again = service._settle(
            row, result.copy(), self.provider, "not persisted", [], [], rate
        )
        row.invalidate_recordset()
        self.assertEqual(status, "overage")
        self.assertEqual(status_again, "overage")
        self.assertEqual(settled["cost"], 6.25)
        self.assertEqual(settled_again["cost"], 6.25)
        self.assertEqual(row.state, "overage")
        self.assertEqual(row.charged_amount, 6.25)
        self.assertEqual(row.rate_version, rate.version)
        self.assertEqual(row.rate_currency_id, self.env.ref("base.EUR"))
        self.assertEqual(row.fx_rate, 1.25)
        self.assertEqual(row.usage_status, "reported")
        self.assertEqual(row.usage_provenance, "provider")
        self.assertEqual(row.cost_status, "estimated")
        self.assertEqual(row.cost_provenance, "rate_card")

    def test_rate_currency_and_fx_are_snapshotted_for_reservation(self):
        company = self.agent_user.company_id
        run = self.make_goal_run([])
        rate = self.env["ai.agent.usage.rate"].create(
            {
                "provider_kind": "stub",
                "model": "stub-model",
                "input_per_million": 1.0,
                "output_per_million": 2.0,
                "currency_id": self.env.ref("base.EUR").id,
                "request_ceiling": 5.0,
            }
        )
        service = self.env["ai.agent.usage.attempt"]
        with patch.object(
            type(rate.currency_id), "_get_conversion_rate", return_value=1.25
        ):
            snapshot = service._reservation_values(
                run,
                self.provider,
                rate,
                date(2026, 10, 1),
                fields.Datetime.now(),
            )
        self.assertEqual(snapshot["rate_version"], rate.version)
        self.assertEqual(snapshot["rate_currency_id"], self.env.ref("base.EUR").id)
        self.assertEqual(snapshot["company_currency_id"], company.currency_id.id)
        self.assertEqual(snapshot["fx_rate"], 1.25)
        self.assertEqual(snapshot["request_ceiling"], 6.25)

    def test_usage_reads_follow_agent_and_company_scope(self):
        run = self.make_goal_run([{"kind": "final", "answer": "done"}])
        run.action_run_to_completion()
        other = new_test_user(
            self.env, login="other_usage_reader", groups="base.group_user"
        )
        count = (
            self.env["ai.agent.usage.attempt"]
            .with_user(other)
            .search_count([("run_db_id", "=", run.id)])
        )
        self.assertEqual(count, 0)
