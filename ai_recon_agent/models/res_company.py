from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


class ResCompany(models.Model):
    _inherit = "res.company"

    ai_recon_agent_enabled = fields.Boolean(
        string="Enable attributed reconciliation matching",
        help="Opt this company into immutable, agent-attributed matching proposals.",
    )
    ai_recon_agent_provider_id = fields.Many2one(
        "ai.agent.provider", string="Reconciliation assessment provider"
    )
    ai_recon_agent_disclosure_version = fields.Char(
        default="reconciliation-candidates-v1", required=True
    )
    ai_recon_agent_max_candidates = fields.Integer(default=50, required=True)
    ai_recon_agent_max_allocations = fields.Integer(default=10, required=True)

    @api.constrains("ai_recon_agent_max_candidates", "ai_recon_agent_max_allocations")
    def _check_ai_recon_limits(self):
        for company in self:
            if (
                company.ai_recon_agent_max_candidates < 1
                or company.ai_recon_agent_max_allocations < 1
                or company.ai_recon_agent_max_allocations
                > company.ai_recon_agent_max_candidates
            ):
                raise ValidationError(
                    self.env._(
                        "Reconciliation candidate limits must be positive, and the "
                        "allocation limit cannot exceed the candidate limit."
                    )
                )

    @api.model
    def _check_ai_recon_admin(self):
        if not self.env.user.has_group("base.group_system") or self.env[
            "ai.agent"
        ].sudo().with_context(active_test=False).search_count(
            [("user_id", "=", self.env.uid)]
        ):
            raise AccessError(
                self.env._("Only human administrators can configure this profile.")
            )

    @api.model_create_multi
    def create(self, vals_list):
        protected = {
            "ai_recon_agent_enabled",
            "ai_recon_agent_provider_id",
            "ai_recon_agent_disclosure_version",
            "ai_recon_agent_max_candidates",
            "ai_recon_agent_max_allocations",
            "ai_recon_reviewer_ids",
        }
        if any(protected.intersection(vals) for vals in vals_list):
            self._check_ai_recon_admin()
        return super().create(vals_list)

    def write(self, vals):
        if {
            "ai_recon_agent_enabled",
            "ai_recon_agent_provider_id",
            "ai_recon_agent_disclosure_version",
            "ai_recon_agent_max_candidates",
            "ai_recon_agent_max_allocations",
            "ai_recon_reviewer_ids",
        }.intersection(vals):
            self._check_ai_recon_admin()
        return super().write(vals)
