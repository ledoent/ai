from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    ai_recon_reviewer_ids = fields.Many2many(
        related="company_id.ai_recon_reviewer_ids", readonly=False
    )
