# pylint: disable=method-required-super,no-raise-unlink
from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


class AiAgentUsageRate(models.Model):
    _name = "ai.agent.usage.rate"
    _description = "AI Provider Usage Rate Card"
    _order = "provider_kind, model, version desc"

    name = fields.Char(compute="_compute_name")
    provider_kind = fields.Selection(
        [("anthropic", "Anthropic"), ("openai", "OpenAI"), ("stub", "Stub")],
        required=True,
    )
    model = fields.Char(required=True, index=True)
    version = fields.Integer(required=True, readonly=True)
    input_per_million = fields.Float(
        required=True,
        digits=(16, 8),
        help="Price per million input tokens in the selected rate currency.",
    )
    output_per_million = fields.Float(
        required=True,
        digits=(16, 8),
        help="Price per million output tokens in the selected rate currency.",
    )
    currency_id = fields.Many2one("res.currency", required=True, ondelete="restrict")
    request_ceiling = fields.Monetary(
        required=True,
        currency_field="currency_id",
        help="Maximum amount reserved before a request is sent.",
    )
    created_by = fields.Many2one(
        "res.users", required=True, default=lambda self: self.env.user, readonly=True
    )
    created_at = fields.Datetime(
        required=True, default=fields.Datetime.now, readonly=True
    )
    _unique_rate_version = models.Constraint(
        "UNIQUE(provider_kind, model, version)",
        "A provider/model rate-card version already exists.",
    )

    @api.depends("provider_kind", "model", "version")
    def _compute_name(self):
        for record in self:
            record.name = f"{record.provider_kind} / {record.model} / v{record.version}"

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(self.env._("Only administrators can create rate cards."))
        vals_list = [dict(vals) for vals in vals_list]
        versions = {}
        for vals in vals_list:
            if "version" in vals or "created_by" in vals or "created_at" in vals:
                raise AccessError(self.env._("Rate-card versions are server-owned."))
            key = (vals.get("provider_kind"), vals.get("model"))
            if key not in versions:
                lock_key = f"ai-agent-rate:{key[0]}:{key[1]}"
                self.env.cr.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)", [lock_key]
                )
                previous = self.search(
                    [("provider_kind", "=", key[0]), ("model", "=", key[1])],
                    order="version desc",
                    limit=1,
                )
                versions[key] = previous.version if previous else 0
            versions[key] += 1
            vals["version"] = versions[key]
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError(
            self.env._("Rate cards are immutable; create a new version instead.")
        )

    def unlink(self):
        raise AccessError(self.env._("Rate-card history cannot be deleted."))

    @api.constrains("input_per_million", "output_per_million", "request_ceiling")
    def _check_nonnegative_prices(self):
        if any(
            r.input_per_million < 0
            or r.output_per_million < 0
            or r.request_ceiling <= 0
            for r in self
        ):
            raise ValidationError(
                self.env._(
                    "Prices must be nonnegative and the request ceiling positive."
                )
            )
