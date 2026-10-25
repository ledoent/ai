# pylint: disable=method-required-super,no-raise-unlink,context-overridden
import hashlib
import json

from odoo import api, models
from odoo.exceptions import AccessError


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


class Protected(models.AbstractModel):
    _name = "ai.recon.protected"
    _description = "Protected Matching Record"

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(
            self.env._("Matching records are created by validated server actions.")
        )

    def write(self, vals):
        raise AccessError(
            self.env._("Matching snapshots and lifecycle fields are protected.")
        )

    def unlink(self):
        raise AccessError(self.env._("Matching history cannot be deleted."))

    @api.model
    def _new(self, vals):
        return super(Protected, self.sudo().with_context({})).create(vals)

    def _set(self, vals):
        return super(Protected, self.sudo()).write(vals)
