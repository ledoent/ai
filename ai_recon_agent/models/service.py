from odoo import api, models
from odoo.exceptions import UserError

from odoo.addons.ai_agent_runtime.invocation import claim


class MatchService(models.AbstractModel):
    _name = "ai.recon.match.service"
    _description = "Invocation-bound Accounting Matcher"

    @api.model
    def match_items(self, proposal_id, payload_hash):
        claim(
            self,
            "match_items",
            {"proposal_id": proposal_id, "payload_hash": payload_hash},
        )
        raise UserError(self.env._("Accounting adapter is not activated."))

    @api.model
    def post_match(self, proposal_id, payload_hash):
        claim(
            self,
            "post_match",
            {"proposal_id": proposal_id, "payload_hash": payload_hash},
        )
        raise UserError(self.env._("Accounting adapter is not activated."))
