from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["ai.agent"].with_context(active_test=False).search([])._sync_grants()
