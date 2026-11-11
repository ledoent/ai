import json

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ..adapters import ReversalAdapter


class AiAgentTool(models.Model):
    """One curated, named capability an agent may be granted.

    This exists because of a measured finding rather than a preference.
    ``ai_agent_surface`` shows that a plain internal user can reach ~1900
    methods, and that **method visibility does not vary by user** -- once a user
    can read a model, every public method on it is callable. So Odoo's access
    rights can express *"may touch partners"* but cannot express *"may read
    partners but not delete them"*. Authorisation at agent granularity has to
    live above the ACL layer, and this model is that layer.
    """

    _name = "ai.agent.tool"
    _description = "AI Agent Tool"
    _order = "model_name, method_name"

    name = fields.Char(required=True)
    description = fields.Text(
        required=True,
        help="What this tool does, in the words you would use to a colleague. "
        "This is the text a model would be shown when choosing a tool.",
    )
    model_name = fields.Char("Model", required=True)
    method_name = fields.Char("Method", required=True)
    requires_approval = fields.Boolean(
        default=False,
        help="Pause the run and wait for a human before this tool is executed.",
    )
    classification = fields.Char(
        compute="_compute_classification",
        help="read-only / writing / irreversible, as reported by ai_agent_surface.",
    )

    _unique_target = models.Constraint(
        "UNIQUE(model_name, method_name)",
        "A tool already exists for that model and method.",
    )

    @api.depends("model_name", "method_name")
    def _compute_classification(self):
        Surface = self.env["ai.agent.surface"]
        for tool in self:
            if tool.model_name in self.env and tool.method_name:
                record = self.env[tool.model_name]
                tool.classification = Surface._classify(record, tool.method_name)
            else:
                tool.classification = False

    @api.constrains("model_name", "method_name")
    def _check_target_is_callable(self):
        """Refuse a tool that ``/json/2`` itself would not dispatch to.

        Reuses ``ai_agent_surface``'s predicate rather than re-deriving the
        rules, so a tool can never be defined for a private method, a
        classmethod, a field, or anything ``@api.private``. Without this a tool
        row pointing at ``sudo`` would look perfectly valid until it ran.
        """
        Surface = self.env["ai.agent.surface"]
        for tool in self:
            if tool.model_name not in self.env:
                raise ValidationError(
                    self.env._("Model %(model)s does not exist.", model=tool.model_name)
                )
            record = self.env[tool.model_name]
            if not Surface._is_callable_remotely(record, tool.method_name):
                raise ValidationError(
                    self.env._(
                        "%(model)s.%(method)s is not a remotely callable method, "
                        "so it cannot be exposed as a tool.",
                        model=tool.model_name,
                        method=tool.method_name,
                    )
                )

    def _undo_adapter(self):
        self.ensure_one()
        if self.model_name not in self.env:
            return None
        record = self.env[self.model_name]
        hook = getattr(record, "_ai_reversal_adapter", None)
        adapter = hook(self.method_name) if hook else None
        if (
            isinstance(adapter, ReversalAdapter)
            and adapter.target == (self.model_name, self.method_name)
            and adapter.contract_id
            and adapter.database_local
        ):
            return adapter
        return None

    def _policy_signature(self):
        self.ensure_one()
        adapter = self._undo_adapter()
        signature = [
            self.model_name,
            self.method_name,
            self.classification,
            self.requires_approval,
            adapter.contract_id if adapter else None,
        ]
        if adapter and adapter.business_evidence_required:
            signature.append("business-evidence-required")
        return json.dumps(signature)

    @api.model_create_multi
    def create(self, vals_list):
        self.env["ai.agent"]._check_admin()
        return super().create(vals_list)

    def write(self, vals):
        self.env["ai.agent"]._check_admin()
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM ai_agent_tool WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(self.ids)],
        )
        result = super().write(vals)
        if {"model_name", "method_name", "requires_approval"}.intersection(vals):
            caps = (
                self.env["ai.agent.capability"]
                .sudo()
                .search([("tool_id", "in", self.ids)])
            )
            caps._change("Tool target or approval policy changed.", level="observe")
        return result

    def unlink(self):
        self.env["ai.agent"]._check_admin()
        return super().unlink()
