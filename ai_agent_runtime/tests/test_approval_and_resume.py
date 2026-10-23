import json

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import AgentRuntimeCommon


@tagged("post_install", "-at_install")
class TestApprovalGate(AgentRuntimeCommon):
    """A run pauses *before* acting, and an approval authorises one step only."""

    def test_run_pauses_before_executing_and_changes_nothing(self):
        before = self.env["res.partner"].search_count([])
        run = self.make_run(("create_partner", {"vals_list": [{"name": "Gated"}]}))
        run._advance()
        self.assertEqual(run.state, "awaiting_approval")
        self.assertEqual(run.pending_tool_id, self.tool_create)
        # The crucial part: nothing happened yet.
        self.assertFalse(run.step_ids)
        self.assertEqual(self.env["res.partner"].search_count([]), before)

    def test_advance_while_awaiting_is_a_no_op(self):
        """A paused run cannot be nudged forward by re-draining."""
        run = self.make_run(("create_partner", {"vals_list": [{"name": "Gated"}]}))
        run._advance()
        for _ in range(3):
            self.assertFalse(run._advance())
        self.assertEqual(run.state, "awaiting_approval")
        self.assertFalse(run.step_ids)

    def test_approval_executes_exactly_the_pending_step(self):
        run = self.make_run(("create_partner", {"vals_list": [{"name": "Approved"}]}))
        run._advance()
        run.action_approve()
        self.assertEqual(run.approver_id, self.env.user)
        run.action_run_to_completion()
        self.assertEqual(run.state, "done")
        self.assertEqual(len(run.step_ids), 1)
        created = self.env["res.partner"].search([("name", "=", "Approved")])
        self.assertEqual(len(created), 1)
        # The partner was created by the agent's user, not by us or by root.
        self.assertEqual(created.create_uid, self.agent_user)

    def test_approval_does_not_carry_to_the_next_gated_step(self):
        """Two gated steps require two approvals.

        Otherwise one click would authorise an unbounded tail of actions, which
        is the failure mode an approval gate exists to prevent.
        """
        run = self.make_run(
            ("create_partner", {"vals_list": [{"name": "First"}]}),
            ("create_partner", {"vals_list": [{"name": "Second"}]}),
        )
        run._advance()
        run.action_approve()
        run.action_run_to_completion()
        # Stopped again at the second gated step rather than running on.
        self.assertEqual(run.state, "awaiting_approval")
        self.assertEqual(len(run.step_ids), 1)
        self.assertFalse(run.approver_id)
        self.assertEqual(
            self.env["res.partner"].search_count([("name", "=", "Second")]), 0
        )

    def test_rejection_cancels_without_acting(self):
        run = self.make_run(("create_partner", {"vals_list": [{"name": "Rejected"}]}))
        run._advance()
        run.action_reject()
        self.assertEqual(run.state, "cancelled")
        self.assertFalse(run.step_ids)
        self.assertEqual(
            self.env["res.partner"].search_count([("name", "=", "Rejected")]), 0
        )


@tagged("post_install", "-at_install")
class TestResume(AgentRuntimeCommon):
    """Durability: the cursor and the audit trail cannot disagree."""

    def test_steps_execute_in_order_one_at_a_time(self):
        run = self.make_run(
            ("count_partners", {"domain": []}), ("count_partners", {"domain": []})
        )
        self.assertTrue(run._advance())
        self.assertEqual(run.step_no, 1)
        self.assertEqual(len(run.step_ids), 1)
        self.assertFalse(run._advance())
        self.assertEqual(run.state, "done")
        self.assertEqual(run.step_ids.mapped("sequence"), [0, 1])

    def test_resuming_a_partly_done_run_does_not_repeat_work(self):
        """The resume path, which is what a crash actually looks like.

        A killed worker leaves the run in 'running' with the cursor already
        advanced past the committed steps. Re-draining must continue, not
        restart -- so the step count equals the plan length, not more.
        """
        run = self.make_run(*[("count_partners", {"domain": []})] * 4)
        run._advance()
        run._advance()
        self.assertEqual(run.step_no, 2)
        # Simulate a fresh worker picking the same run up again.
        run.invalidate_recordset()
        run.action_run_to_completion()
        self.assertEqual(run.state, "done")
        self.assertEqual(len(run.step_ids), 4)
        self.assertEqual(run.step_ids.mapped("sequence"), [0, 1, 2, 3])

    def test_a_step_cannot_be_recorded_twice(self):
        """The guard that makes idempotency structural rather than hopeful."""
        from psycopg2.errors import UniqueViolation

        from odoo.tools import mute_logger

        run = self.make_run(("count_partners", {"domain": []}))
        run._advance()
        with (
            self.assertRaises(UniqueViolation),
            mute_logger("odoo.sql_db"),
            self.cr.savepoint(),
        ):
            self.env["ai.agent.run.step"].create(
                {
                    "run_id": run.id,
                    "sequence": 0,
                    "tool_id": self.tool_count.id,
                    "args": "{}",
                }
            )
            self.env.flush_all()

    def test_empty_plan_completes_immediately(self):
        run = self.make_run()
        self.assertFalse(run._advance())
        self.assertEqual(run.state, "done")
        self.assertFalse(run.step_ids)

    def test_cron_drain_advances_running_runs_only(self):
        """The drain picks up 'running' and leaves everything else alone.

        ``_commit_progress`` is patched out because it calls ``cr.commit()``,
        which ``TransactionCase`` forbids outright
        (``odoo/tests/common.py:1386``). Returning a positive budget keeps the
        loop's ``if not remaining_time: break`` behaving as it does in
        production. What is under test here is the selection and the advance,
        not core's commit.
        """
        running = self.make_run(("count_partners", {"domain": []}))
        draft = self.Run.create(
            {
                "agent_id": self.agent.id,
                "plan": json.dumps(
                    [{"tool": "count_partners", "args": {"domain": []}}]
                ),
            }
        )
        self.patch(
            type(self.env["ir.cron"]),
            "_commit_progress",
            lambda self, processed=0, **kw: 60.0,
        )
        self.Run._cron_drain()
        self.assertEqual(running.state, "done")
        self.assertEqual(len(running.step_ids), 1)
        self.assertEqual(draft.state, "draft")
        self.assertFalse(draft.step_ids)

    def test_drain_survives_one_bad_run(self):
        """A failing run must not abort the queue behind it."""
        bad = self.make_run(("count_partners", {"domain": [], "bogus": 1}))
        good = self.make_run(("count_partners", {"domain": []}))
        self.patch(
            type(self.env["ir.cron"]),
            "_commit_progress",
            lambda self, processed=0, **kw: 60.0,
        )
        self.Run._cron_drain()
        self.assertEqual(bad.state, "failed")
        self.assertEqual(good.state, "done")


@tagged("post_install", "-at_install")
class TestBadPlan(AgentRuntimeCommon):
    """A malformed plan fails the run cleanly instead of escaping it.

    Once a planner rather than a human writes the plan, wrong arguments become
    the most likely defect in the system -- so they have to produce an
    actionable run failure, not a traceback. Mirrors what ``/json/2`` does by
    pre-binding the signature (``json2.py:80-84`` -> 422).
    """

    def test_unknown_argument_fails_the_run_with_a_message(self):
        # The valid argument has to be present too: signature.bind reports the
        # *first* problem it finds, so a missing required argument would mask
        # the unexpected one.
        run = self.make_run(("count_partners", {"domain": [], "nonsense_kwarg": 1}))
        run._advance()
        self.assertEqual(run.state, "failed")
        self.assertIn("nonsense_kwarg", run.error)
        self.assertEqual(len(run.step_ids), 1)
        self.assertTrue(run.step_ids.failed)

    def test_missing_required_argument_fails_the_run(self):
        run = self.make_run(("count_partners", {}))
        run._advance()
        self.assertEqual(run.state, "failed")
        self.assertIn("domain", run.error)

    def test_unparseable_plan_is_a_user_error(self):
        run = self.Run.create({"agent_id": self.agent.id, "plan": "not json"})
        run.action_start()
        with self.assertRaises(UserError):
            run._advance()

    def test_plan_that_is_not_a_list_is_a_user_error(self):
        run = self.Run.create({"agent_id": self.agent.id, "plan": '{"tool": "x"}'})
        run.action_start()
        with self.assertRaises(UserError):
            run._advance()
