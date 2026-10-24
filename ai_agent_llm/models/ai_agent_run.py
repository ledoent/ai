import json
import logging

from odoo import fields, models
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)


class AiAgentRun(models.Model):
    """Adds the goal-driven loop on top of the durable static-plan runtime.

    The difference from a plan is the whole point. A static plan decides
    everything up front; a loop lets the model see each result and choose the
    next action, which is what makes an agent able to look an identifier up
    before using it rather than guessing. Everything that made the static
    runtime safe is reused unchanged -- the allowlist, the agent's identity, the
    approval gate, the step records, the resume cursor. The model gets to pick
    *which* tool; it gets no say in whether it is allowed.
    """

    _inherit = "ai.agent.run"

    goal = fields.Text(
        help="What the agent should accomplish, in plain language. When set, "
        "the run is driven by the provider instead of by the plan."
    )
    answer = fields.Text(readonly=True)

    def _is_goal_driven(self):
        self.ensure_one()
        return bool(self.goal and self.agent_id.provider_id)

    def _transcript(self):
        """What has happened so far, oldest first, for the provider."""
        self.ensure_one()
        return [
            {
                "tool": step.tool_id.name,
                "args": step.args,
                "result": step.result or "",
            }
            for step in self.step_ids.sorted("sequence")
        ]

    def _advance(self):
        """Goal-driven runs take the loop; everything else keeps the plan path."""
        self.ensure_one()
        if not self._is_goal_driven():
            return super()._advance()
        if self.state != "running":
            return False

        # A budget, not an optimisation. Without it a model that keeps choosing
        # tools runs until something else stops it, and "something else" on a
        # production ERP is an unbounded number of writes.
        if self.step_no >= self.agent_id.max_steps:
            self.write(
                {
                    "state": "failed",
                    "error": self.env._(
                        "Step budget of %(n)s exhausted without reaching an answer.",
                        n=self.agent_id.max_steps,
                    ),
                }
            )
            return False

        # A step already approved is executed without asking the model again --
        # otherwise an approval could authorise a *different* action than the
        # one the human saw.
        if self.pending_tool_id and self.approver_id:
            return self._execute_pending()

        decision = self.agent_id.provider_id._decide(
            self.goal, self._transcript(), self.agent_id._tool_schemas()
        )

        if decision.get("kind") == "final":
            self.write({"state": "done", "answer": decision.get("answer") or ""})
            return False

        try:
            tool = self._resolve_tool(decision.get("tool"))
        except AccessError as exc:
            # The model asked for something outside the allowlist. That is a
            # normal event, not a crash: record it and stop. A production
            # design might feed the refusal back and let it retry; failing
            # closed is the right default until that is measured.
            self.write({"state": "failed", "error": str(exc)})
            return False

        args = decision.get("args") or {}
        if tool.requires_approval:
            self.write(
                {
                    "state": "awaiting_approval",
                    "pending_tool_id": tool.id,
                    "pending_args": json.dumps(args),
                }
            )
            return False

        return self._run_step(tool, args)

    def _annotate_result(self, tool, args, result):
        """Render a result, telling the model when its view was truncated.

        This exists because of a measured failure, not a hypothesis. Asked *"how
        many posted customer invoices are unpaid, and what do they total?"*
        against a real multi-company production copy, a model made two correct
        tool calls and answered *"1 invoice, 7097.74"* — fluently, precisely,
        and wrong. The real answer was 3 and 10597.74; the other two belonged to
        a company the agent's user cannot see. Odoo behaved exactly as designed.
        The defect was ours: the wrapper handed over a filtered count with
        nothing to indicate it had been filtered.

        Note what this does *not* do. ``sudo()`` cannot tell you what a user
        would have been refused — every access predicate short-circuits under
        ``env.su``, which ``ai_agent_surface`` demonstrates. But running the same
        domain twice, once per identity, *can*. So the audit mode that is
        impossible to get from one privileged call is available from two calls,
        and that is the whole trick.

        Opt-in per tool, because it costs a second query and because on a tool
        that is already unrestricted it would be pure overhead.
        """
        self.ensure_one()
        rendered = repr(result)
        if not tool.report_truncation:
            return rendered
        domain = args.get("domain")
        if not isinstance(domain, list):
            return rendered
        try:
            Model = self.env[tool.model_name]
            visible = Model.with_user(self.agent_id.user_id).search_count(domain)
            total = Model.sudo().search_count(domain)
        except Exception:  # noqa: BLE001 - an annotation must never break a call
            return rendered
        if total <= visible:
            return rendered
        return (
            f"{rendered}\n"
            f"NOTE: this result covers {visible} of {total} records matching the "
            f"domain. {total - visible} are excluded by this user's access "
            f"rights, so any total or ranking derived from it is partial. Say so "
            f"in your answer rather than presenting it as complete."
        )

    def _execute_pending(self):
        self.ensure_one()
        tool = self.pending_tool_id
        try:
            args = json.loads(self.pending_args or "{}")
        except ValueError:
            args = {}
        return self._run_step(tool, args)

    def _run_step(self, tool, args):
        """Execute one tool call and record it.

        Two things here are not obvious and were both found by watching a real
        model fail.

        **The catch is broad on purpose.** A model that invents a field name --
        ``('type', '=', 'out_invoice')`` instead of ``move_type``, the pre-13.0
        name it was presumably trained on -- makes the ORM raise ``ValueError``,
        not ``UserError``. Core maps that to a 500 on ``/json/2`` because for an
        RPC client it *is* a server error; inside an agent loop it is ordinary,
        recoverable feedback. Narrow catches let it escape the run instead, and
        the model never gets the chance to correct a one-word mistake. The step
        budget is what keeps this from looping forever.

        **The savepoint is load-bearing.** Some failures poison the cursor --
        any SQL-level error leaves the transaction unusable, so the very next
        write (recording the failed step) would itself fail and take the whole
        run down. Rolling back to a savepoint keeps the failure contained to the
        one tool call.
        """
        self.ensure_one()
        Step = self.env["ai.agent.run.step"]
        # Snapshot the arguments BEFORE the call. Odoo's create() mutates the
        # vals dict in place -- observed on real data, where res.partner.create
        # added properties_base_definition_id -- so serialising afterwards
        # records arguments that differ from the ones that were approved. An
        # approval trail that misreports what was approved defeats the gate.
        args_snapshot = json.dumps(args)
        try:
            with self.env.cr.savepoint():
                result = self._execute_tool(tool, args)
        except Exception as exc:  # noqa: BLE001 -- see docstring
            # INFO, not WARNING: an expected, recoverable event, and OCA CI
            # turns any WARNING into a build failure.
            _logger.info(
                "ai.agent.run %s: tool %s failed, feeding back to the model: %s",
                self.id,
                tool.name,
                exc,
            )
            Step.create(
                {
                    "run_id": self.id,
                    "sequence": self.step_no,
                    "tool_id": tool.id,
                    "args": args_snapshot,
                    "result": f"{type(exc).__name__}: {exc}"[:2000],
                    "failed": True,
                }
            )
            # Unlike the static plan, a failed step does not end a goal-driven
            # run: the failure is in the transcript, so the model can see what
            # went wrong and try something else.
            self.write(
                {
                    "step_no": self.step_no + 1,
                    "approver_id": False,
                    "pending_tool_id": False,
                    "pending_args": False,
                }
            )
            return True

        Step.create(
            {
                "run_id": self.id,
                "sequence": self.step_no,
                "tool_id": tool.id,
                "args": args_snapshot,
                "result": self._annotate_result(tool, args, result)[:2000],
            }
        )
        self.write(
            {
                "step_no": self.step_no + 1,
                "approver_id": False,
                "pending_tool_id": False,
                "pending_args": False,
            }
        )
        return True
