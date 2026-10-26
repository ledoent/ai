from odoo import api, models


class AiReconBatch(models.Model):
    _inherit = "ai.recon.batch"

    @api.model
    def _candidate_domain(self):
        return super()._candidate_domain() + [
            ("company_id.ai_recon_agent_enabled", "=", False)
        ]
