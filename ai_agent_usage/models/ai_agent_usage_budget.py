# pylint: disable=no-raise-unlink
from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


class AiAgentUsageBudget(models.Model):
    _name = "ai.agent.usage.budget"
    _description = "AI Agent Monthly Budget"
    _order = "company_id, agent_id"
    _check_company_auto = True

    company_id = fields.Many2one("res.company", required=True, ondelete="cascade")
    agent_id = fields.Many2one("ai.agent", required=True, ondelete="cascade")
    currency_id = fields.Many2one(related="company_id.currency_id", readonly=True)
    monthly_limit = fields.Monetary(
        currency_field="currency_id",
        help="Agent monthly cap for runs billed to this company; zero means no cap.",
    )
    run_limit = fields.Monetary(
        currency_field="currency_id",
        help="Default per-run cap for this company; zero means no cap.",
    )

    _unique_agent_company = models.Constraint(
        "UNIQUE(company_id, agent_id)",
        "An agent budget already exists for this company.",
    )

    @api.model
    def _lock_companies(self, company_ids):
        company_ids = sorted({company_id for company_id in company_ids if company_id})
        for company_id in company_ids:
            self.env.cr.execute(
                """UPDATE res_company
                      SET ai_usage_lock_version = ai_usage_lock_version + 1
                    WHERE id = %s""",
                [company_id],
            )

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(self.env._("Only administrators can configure budgets."))
        self._lock_companies(vals.get("company_id") for vals in vals_list)
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(self.env._("Only administrators can configure budgets."))
        self._lock_companies(
            [*self.mapped("company_id").ids, vals.get("company_id", False)]
        )
        return super().write(vals)

    def unlink(self):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(self.env._("Only administrators can configure budgets."))
        self._lock_companies(self.mapped("company_id").ids)
        return super().unlink()

    @api.constrains("monthly_limit", "run_limit")
    def _check_limits(self):
        if any(b.monthly_limit < 0 or b.run_limit < 0 for b in self):
            raise ValidationError(self.env._("AI budget limits cannot be negative."))
