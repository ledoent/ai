from odoo import api, models
from odoo.exceptions import UserError


class LegacyBatch(models.Model):
    _inherit = "ai.recon.batch"

    @api.model
    def _candidate_domain(self):
        companies = (
            self.env["ai.recon.profile"]
            .sudo()
            .search([("enabled", "=", True)])
            .company_id
        )
        return super()._candidate_domain() + [("company_id", "not in", companies.ids)]


class LegacyProposal(models.Model):
    _inherit = "ai.recon.proposal"

    def _assert_preconditions(self):
        companies = (
            self.env["ai.recon.profile"]
            .sudo()
            .search([("enabled", "=", True)])
            .company_id
        )
        if self.company_id & companies:
            raise UserError(
                self.env._(
                    "This company uses managed matching. Use a fresh matching assessment or native accounting; legacy feedback earns no authority."
                )
            )
        return super()._assert_preconditions()
