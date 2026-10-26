import logging

from odoo import fields, models

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
        return bool(self.goal and self.provider_snapshot_id)

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

    def _control_fields(self):
        return super()._control_fields() | {
            "answer",
            "provider_snapshot_id",
            "step_budget",
        }

    def _provider_decision(self):
        """Select the next action through the provider integration hook."""
        self.ensure_one()
        return self.provider_snapshot_id._decide(
            self.goal, self._transcript(), self.agent_id._tool_schemas()
        )

    def _before_provider_decision(self):
        """Allow optional addons to pause a run before its next provider call."""
        return False

    provider_snapshot_id = fields.Many2one("ai.agent.provider", readonly=True)
    step_budget = fields.Integer(readonly=True)

    def _start_values(self):
        return dict(
            super()._start_values(),
            provider_snapshot_id=self.agent_id.provider_id.id,
            step_budget=self.agent_id.max_steps,
        )

    def _advance(self):
        self.ensure_one()
        self._lock()
        if not self._is_goal_driven():
            return super()._advance()
        if self.state != "running":
            return False
        if self.pending_call_id:
            return self._dispatch(recover=True)
        if self.step_no >= self.step_budget:
            self._set({"state": "failed", "error": "Step budget exhausted."})
            return False
        if self._before_provider_decision():
            return False
        decision = self._provider_decision()
        if decision is None:
            return False
        if decision.get("kind") == "final":
            self._set({"state": "done", "answer": decision.get("answer") or ""})
            return False
        return self._dispatch(
            decision.get("tool"), decision.get("args") or {}, recover=True
        )

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
