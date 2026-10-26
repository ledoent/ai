from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError


class AiAgentRun(models.Model):
    _inherit = "ai.agent.run"

    state = fields.Selection(
        selection_add=[("usage_hold", "Usage hold")],
        ondelete={"usage_hold": "set default"},
    )
    billing_company_id = fields.Many2one(
        "res.company",
        domain="[('id', 'in', company_ids)]",
        help="The single company charged for all provider requests in this run. "
        "Required when execution spans more than one company.",
    )
    usage_currency_id = fields.Many2one(
        related="billing_company_id.currency_id", readonly=True
    )
    usage_run_limit = fields.Monetary(
        compute="_compute_usage_run_limit",
        currency_field="usage_currency_id",
        readonly=True,
        help="Current per-run cap for this company. Budget edits take effect on "
        "the next request after an administrator explicitly resumes a held run.",
    )
    usage_provider_kind_snapshot = fields.Char(readonly=True)
    usage_model_snapshot = fields.Char(readonly=True)
    usage_provider_name_snapshot = fields.Char(readonly=True)
    usage_resume_after_attempt = fields.Integer(readonly=True)
    usage_hold_reason = fields.Text(readonly=True)
    usage_pause_after_dispatch = fields.Boolean(readonly=True)

    def _control_fields(self):
        return super()._control_fields() | {
            "usage_provider_kind_snapshot",
            "usage_model_snapshot",
            "usage_provider_name_snapshot",
            "usage_resume_after_attempt",
            "usage_hold_reason",
            "usage_pause_after_dispatch",
        }

    @api.depends(
        "billing_company_id",
        "agent_id",
        "agent_id.usage_budget_ids.run_limit",
    )
    def _compute_usage_run_limit(self):
        Budget = self.env["ai.agent.usage.budget"].sudo()
        for run in self:
            budget = Budget.search(
                [
                    ("company_id", "=", run.billing_company_id.id),
                    ("agent_id", "=", run.agent_id.id),
                ],
                limit=1,
            )
            run.usage_run_limit = budget.run_limit if budget else 0.0

    def _start_values(self):
        self.ensure_one()
        vals = super()._start_values()
        user = self.agent_id.user_id
        companies = self.company_ids or user.company_id
        billing_company = self.billing_company_id
        if not billing_company and len(companies) == 1:
            billing_company = companies
        if not billing_company or billing_company not in companies:
            raise UserError(
                self.env._(
                    "Select one billing company from this run's execution companies."
                )
            )
        provider = self.agent_id.provider_id
        vals.update(
            {
                "billing_company_id": billing_company.id,
                "usage_provider_kind_snapshot": provider.kind if provider else False,
                "usage_model_snapshot": provider.model if provider else False,
                "usage_provider_name_snapshot": provider.name if provider else False,
                "usage_resume_after_attempt": 0,
                "usage_hold_reason": False,
                "usage_pause_after_dispatch": False,
            }
        )
        return vals

    def _before_provider_decision(self):
        self.ensure_one()
        if super()._before_provider_decision():
            return True
        if self.usage_pause_after_dispatch:
            reason = (
                self.usage_hold_reason
                or "Provider cost exceeded the reserved request ceiling."
            )
            self._set(
                {
                    "state": "usage_hold",
                    "error": reason,
                    "usage_hold_reason": reason,
                    "usage_pause_after_dispatch": False,
                }
            )
            return True
        Attempt = self.env["ai.agent.usage.attempt"].sudo()
        Attempt.invalidate_model(["state", "reservation_state", "error_summary"])
        unresolved = Attempt.search(
            [
                ("run_db_id", "=", self.id),
                ("state", "in", ["sending", "unknown"]),
                ("reservation_state", "=", "held"),
            ],
            order="id desc",
            limit=1,
        )
        pause = unresolved
        if not pause:
            pause = Attempt.search(
                [
                    ("run_db_id", "=", self.id),
                    ("id", ">", self.usage_resume_after_attempt),
                    ("state", "in", ["blocked", "overage"]),
                ],
                order="id desc",
                limit=1,
            )
        if pause:
            reason = (
                pause.error_summary
                or "Resolve the provider usage hold before continuing."
            )
            self._set(
                {
                    "state": "usage_hold",
                    "error": reason,
                    "usage_hold_reason": reason,
                }
            )
            return True
        return False

    def _provider_decision(self):
        self.ensure_one()
        provider = self.provider_snapshot_id.with_context(
            _ai_usage_kind_snapshot=self.usage_provider_kind_snapshot,
            _ai_usage_model_snapshot=self.usage_model_snapshot,
        )
        result = self.env["ai.agent.usage.attempt"]._decide_metered(
            self,
            provider,
            self.goal,
            self._transcript(),
            self.agent_id._tool_schemas(),
        )
        if not result or result.get("usage_hold_reason"):
            reason = result.get("usage_hold_reason") if result else False
            reason = (
                reason or "Provider request is held until an administrator resolves it."
            )
            self._set(
                {
                    "state": "usage_hold",
                    "error": reason,
                    "usage_hold_reason": reason,
                }
            )
            return None
        if result.get("usage_overage") and result["decision"].get("kind") == "tool":
            self._set(
                {
                    "usage_pause_after_dispatch": True,
                    "usage_hold_reason": (
                        "Actual provider cost exceeded the reserved request ceiling."
                    ),
                }
            )
        return result["decision"]

    def action_resume_usage(self):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(self.env._("Only administrators can resume held runs."))
        Attempt = self.env["ai.agent.usage.attempt"].sudo()
        for run in self:
            run._lock()
            if run.state != "usage_hold":
                raise UserError(self.env._("Only a run on usage hold can be resumed."))
            unresolved = Attempt.search_count(
                [
                    ("run_db_id", "=", run.id),
                    ("state", "in", ["sending", "unknown"]),
                    ("reservation_state", "=", "held"),
                ]
            )
            if unresolved:
                raise UserError(
                    self.env._(
                        "Resolve or release every held provider reservation first."
                    )
                )
            latest = Attempt.search(
                [("run_db_id", "=", run.id)], order="id desc", limit=1
            )
            run._set(
                {
                    "state": "running",
                    "error": False,
                    "usage_hold_reason": False,
                    "usage_resume_after_attempt": latest.id if latest else 0,
                    "usage_pause_after_dispatch": False,
                }
            )
        return True
