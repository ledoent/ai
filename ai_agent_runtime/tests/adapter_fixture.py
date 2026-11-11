"""A database-local vertical registered only by tests and the race harness."""

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.orm.model_classes import add_to_registry

from ..adapters import ReversalAdapter


class SetValueAdapter(ReversalAdapter):
    database_local = True
    target = ("ai.runtime.contract.item", "action_set_value")
    contract_id = "set-value-v1"

    def _locked_item(self, record, item_id):
        item = record.browse(item_id)
        item.check_access("write")
        item.flush_recordset()
        record.env.cr.execute(
            "SELECT id FROM ai_runtime_contract_item WHERE id = %s FOR UPDATE",
            [item_id],
        )
        item.invalidate_recordset()
        if not item.exists():
            raise UserError(record.env._("The subject no longer exists."))
        return item

    def prepare(self, record, args):
        item = self._locked_item(record, args["item_id"])
        return {"item_id": item.id, "value": item.value, "revision": item.revision}

    def capture(self, record, prepared, result):
        item = record.browse(prepared["item_id"])
        return dict(prepared, applied_value=item.value, applied_revision=item.revision)

    def reverse(self, record, receipt):
        item = self._locked_item(record, receipt["item_id"])
        if (
            item.value != receipt["applied_value"]
            or item.revision != receipt["applied_revision"]
        ):
            raise UserError(record.env._("Intervening changes prevent safe reversal."))
        item.write({"value": receipt["value"]})


class ConfirmAdapter(SetValueAdapter):
    target = ("ai.runtime.contract.item", "action_confirm")
    contract_id = "confirm-value-v1"


class RuntimeContractItem(models.Model):
    _name = "ai.runtime.contract.item"
    _description = "Runtime Contract Item"
    _module = "ai_agent_runtime"
    _register = False

    # Direct registry fixtures must declare the access-log fields explicitly.
    create_uid = fields.Many2one("res.users", readonly=True)
    create_date = fields.Datetime(readonly=True)
    write_uid = fields.Many2one("res.users", readonly=True)
    write_date = fields.Datetime(readonly=True)
    name = fields.Char(default="Contract subject")
    value = fields.Integer(default=7)
    revision = fields.Integer(default=0)

    def write(self, vals):
        # A monotonically increasing revision catches ABA changes even when
        # write_date has the same transaction timestamp.
        for item in self:
            super(RuntimeContractItem, item).write(
                dict(vals, revision=item.revision + 1)
            )
        return True

    @api.model
    def action_set_value(self, item_id, value, fail=False):
        item = self.browse(item_id)
        item.write({"value": value})
        if fail:
            raise UserError(self.env._("Failure after writing the subject."))
        return item.value

    @api.model
    def action_confirm(self, item_id, value, fail=False):
        return self.action_set_value(item_id, value, fail=fail)

    def _ai_reversal_adapter(self, method):
        if method == "action_set_value":
            return SetValueAdapter()
        if method == "action_confirm":
            return ConfirmAdapter()
        return None


def register_fixture(env):
    registry = env.registry
    name = RuntimeContractItem._name
    add_to_registry(registry, RuntimeContractItem)
    registry._setup_models__(env.cr, [name])
    registry.init_models(env.cr, [name], {})
    env["ir.access"].create(
        {
            "name": "Contract item access",
            "model_id": env["ir.model"]._get_id(name),
            "group_id": env.ref("base.group_user").id,
            "operation": "crud",
        }
    )


def unregister_fixture(env):
    env.invalidate_all()
    name = RuntimeContractItem._name
    for model in env.registry.models.values():
        model._inherit_children.discard(name)
    env.registry.models.pop(name, None)
