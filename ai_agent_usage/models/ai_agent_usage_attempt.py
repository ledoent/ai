# pylint: disable=method-required-super,no-raise-unlink,context-overridden,invalid-commit
"""Protected provider-attempt ledger and durable reservation service."""

import json
import logging
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from psycopg2 import errors as pg_errors

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import config

_logger = logging.getLogger(__name__)


class AiAgentUsageAttempt(models.Model):
    _name = "ai.agent.usage.attempt"
    _description = "AI Provider Usage Attempt"
    _order = "id desc"

    name = fields.Char(compute="_compute_name")
    attempt_uuid = fields.Char(required=True, readonly=True, index=True)
    # Integer run reference deliberately has no FK: the reservation is committed
    # while the runtime holds that run's row lock across the provider request.
    run_db_id = fields.Integer(required=True, readonly=True, index=True)
    run_id = fields.Many2one(
        "ai.agent.run", compute="_compute_run_id", search="_search_run_id"
    )
    run_name = fields.Char(required=True, readonly=True)
    sequence = fields.Integer(required=True, readonly=True)
    ordinal = fields.Integer(required=True, readonly=True)
    agent_id = fields.Many2one(
        "ai.agent", required=True, readonly=True, ondelete="restrict"
    )
    company_id = fields.Many2one(
        "res.company", required=True, readonly=True, ondelete="restrict"
    )
    provider_kind = fields.Selection(
        [("anthropic", "Anthropic"), ("openai", "OpenAI"), ("stub", "Stub")],
        required=True,
        readonly=True,
    )
    provider_name = fields.Char(readonly=True)
    model = fields.Char(required=True, readonly=True)
    request_id = fields.Char(readonly=True, index=True)
    state = fields.Selection(
        [
            ("reserved", "Reserved"),
            ("sending", "Sending or outcome unknown"),
            ("unknown", "Held for resolution"),
            ("settled", "Settled"),
            ("overage", "Over ceiling"),
            ("blocked", "Blocked before send"),
            ("failed", "Failed before send"),
            ("adjusted", "Admin adjusted"),
            ("released", "Admin released"),
        ],
        required=True,
        readonly=True,
        index=True,
    )
    budget_enforced = fields.Boolean(required=True, readonly=True)
    send_state = fields.Selection(
        [
            ("not_sent", "Not sent"),
            ("sending", "Send may have started"),
            ("received", "Response received"),
        ],
        required=True,
        readonly=True,
    )
    reservation_state = fields.Selection(
        [("held", "Held"), ("settled", "Settled"), ("released", "Released")],
        required=True,
        readonly=True,
        index=True,
    )
    usage_status = fields.Selection(
        [
            ("reported", "Provider reported"),
            ("estimated", "Estimated"),
            ("unavailable", "Unavailable"),
        ],
        required=True,
        readonly=True,
    )
    usage_provenance = fields.Char(readonly=True)
    input_tokens = fields.Integer(readonly=True)
    output_tokens = fields.Integer(readonly=True)
    cost_status = fields.Selection(
        [
            ("reported", "Provider reported"),
            ("estimated", "Estimated"),
            ("unpriced", "Unpriced"),
            ("adjusted", "Admin adjusted"),
        ],
        required=True,
        readonly=True,
    )
    cost_provenance = fields.Selection(
        [
            ("provider", "Provider reported"),
            ("rate_card", "Versioned rate card"),
            ("admin_adjustment", "Admin adjustment"),
            ("unpriced", "Unpriced"),
        ],
        required=True,
        readonly=True,
    )
    provider_cost_currency_id = fields.Many2one(
        "res.currency", readonly=True, ondelete="restrict"
    )
    provider_cost_amount = fields.Monetary(
        currency_field="provider_cost_currency_id", readonly=True
    )
    provider_cost_fx_rate = fields.Float(digits=(16, 10), readonly=True)
    rate_id = fields.Many2one("ai.agent.usage.rate", readonly=True, ondelete="restrict")
    rate_version = fields.Integer(readonly=True)
    rate_currency_id = fields.Many2one(
        "res.currency", readonly=True, ondelete="restrict"
    )
    company_currency_id = fields.Many2one(
        "res.currency", readonly=True, ondelete="restrict"
    )
    fx_rate = fields.Float(digits=(16, 10), readonly=True)
    fx_date = fields.Date(readonly=True)
    request_ceiling = fields.Monetary(
        currency_field="company_currency_id", readonly=True
    )
    reservation_amount = fields.Monetary(
        currency_field="company_currency_id", readonly=True
    )
    held_amount = fields.Monetary(
        currency_field="company_currency_id",
        compute="_compute_held_amount",
        store=True,
    )
    charged_amount = fields.Monetary(
        currency_field="company_currency_id", readonly=True
    )
    budget_period_start = fields.Date(required=True, readonly=True, index=True)
    request_at = fields.Datetime(required=True, readonly=True)
    sent_at = fields.Datetime(readonly=True)
    settled_at = fields.Datetime(readonly=True)
    decision_json = fields.Text(readonly=True, groups="base.group_system")
    error_summary = fields.Char(readonly=True)
    resolution_amount_input = fields.Monetary(
        currency_field="company_currency_id", groups="base.group_system"
    )
    resolution_note_input = fields.Char(groups="base.group_system")
    resolution_action = fields.Selection(
        [("adjust", "Adjusted"), ("release", "Released")], readonly=True
    )
    resolution_note = fields.Text(readonly=True)
    resolved_by = fields.Many2one("res.users", readonly=True, ondelete="restrict")
    resolved_at = fields.Datetime(readonly=True)
    _unique_attempt = models.Constraint(
        "UNIQUE(run_db_id, sequence, ordinal)",
        "A provider attempt already exists for this run decision and ordinal.",
    )
    _unique_uuid = models.Constraint(
        "UNIQUE(attempt_uuid)", "Attempt UUID must be unique."
    )

    @api.depends("reservation_state", "reservation_amount")
    def _compute_held_amount(self):
        for row in self:
            row.held_amount = (
                row.reservation_amount if row.reservation_state == "held" else 0.0
            )

    @api.depends("run_name", "sequence", "ordinal")
    def _compute_name(self):
        for row in self:
            row.name = f"{row.run_name} / request {row.sequence + 1}.{row.ordinal}"

    @api.depends("run_db_id")
    def _compute_run_id(self):
        for row in self:
            row.run_id = self.env["ai.agent.run"].browse(row.run_db_id).exists()

    @api.model
    def _search_run_id(self, operator, value):
        if hasattr(value, "ids"):
            value = value.ids
        if operator in ("=", "!=") and not value:
            return [("run_db_id", operator, 0)]
        return [("run_db_id", operator, value)]

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(self.env._("Provider attempt rows are server-owned."))

    def write(self, vals):
        if (
            vals
            and set(vals).issubset({"resolution_amount_input", "resolution_note_input"})
            and self.env.user.has_group("base.group_system")
            and all(row.state in ("sending", "unknown") for row in self)
        ):
            return super().write(vals)
        raise AccessError(self.env._("Provider attempt history is protected."))

    def unlink(self):
        raise AccessError(self.env._("Provider attempt history cannot be deleted."))

    @api.model
    def _new(self, vals):
        return super(AiAgentUsageAttempt, self.sudo().with_context({})).create(vals)

    def _set(self, vals):
        return super(AiAgentUsageAttempt, self.sudo()).write(vals)

    @api.model
    def _durable(self, callback):
        """Run and commit a small ledger transaction outside the run lock."""
        if config.get("test_enable"):
            # TransactionCase fixtures are intentionally uncommitted. Keeping
            # test operations in that transaction preserves Odoo test isolation.
            return callback(self.env)
        for attempt in range(4):
            try:
                with self.env.registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    result = callback(env)
                    cr.commit()
                    return result
            except (
                pg_errors.SerializationFailure,
                pg_errors.DeadlockDetected,
                pg_errors.UniqueViolation,
            ):
                if attempt == 3:
                    raise

    @api.model
    def _company_month(self, company):
        company = company.sudo()
        zone_name = company.ai_usage_timezone or company.partner_id.tz or "UTC"
        try:
            zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            zone = timezone.utc
        local_date = datetime.now(timezone.utc).astimezone(zone).date()
        return local_date.replace(day=1)

    @api.model
    def _find_rate(self, provider):
        return (
            self.env["ai.agent.usage.rate"]
            .sudo()
            .search(
                [
                    ("provider_kind", "=", provider._effective_kind()),
                    ("model", "=", provider._effective_model()),
                ],
                order="version desc, id desc",
                limit=1,
            )
        )

    @api.model
    def _convert(self, amount, from_currency, company_currency, company, date):
        if not from_currency or not company_currency:
            return 0.0, 0.0
        rate = from_currency._get_conversion_rate(
            from_currency, company_currency, company, date
        )
        return company_currency.round(amount * rate), rate

    @api.model
    def _reservation_values(self, run, provider, rate, period, now):
        company = run.billing_company_id.sudo()
        currency = company.currency_id
        source_currency = rate.currency_id if rate else currency
        source_ceiling = rate.request_ceiling if rate else 0.0
        reservation, fx_rate = self._convert(
            source_ceiling, source_currency, currency, company, now.date()
        )
        return {
            "attempt_uuid": str(uuid.uuid4()),
            "run_db_id": run.id,
            "run_name": run.display_name,
            "sequence": run.step_no,
            "ordinal": 1,
            "agent_id": run.agent_id.id,
            "company_id": company.id,
            "provider_kind": provider._effective_kind(),
            "provider_name": run.usage_provider_name_snapshot or provider.name,
            "model": provider._effective_model() or "(unspecified)",
            "state": "reserved",
            "budget_enforced": bool(company.ai_usage_enabled),
            "send_state": "not_sent",
            "reservation_state": "held" if reservation else "released",
            "usage_status": "unavailable",
            "usage_provenance": "unavailable",
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_status": "unpriced",
            "cost_provenance": "unpriced",
            "provider_cost_currency_id": False,
            "provider_cost_amount": 0.0,
            "provider_cost_fx_rate": 0.0,
            "rate_id": rate.id if rate else False,
            "rate_version": rate.version if rate else 0,
            "rate_currency_id": source_currency.id if source_currency else False,
            "company_currency_id": currency.id,
            "fx_rate": fx_rate,
            "fx_date": now.date(),
            "request_ceiling": reservation,
            "reservation_amount": reservation,
            "charged_amount": 0.0,
            "budget_period_start": period,
            "request_at": now,
        }

    @api.model
    def _reserve(self, run, provider, rate, period):
        vals = self._reservation_values(
            run, provider, rate, period, fields.Datetime.now()
        )
        base_vals = dict(vals)
        rate_id = vals["rate_id"]
        ceiling = vals["reservation_amount"]
        billing_company_id = run.billing_company_id.id
        agent_id = run.agent_id.id
        run_id = run.id

        def reserve(env):
            attempt_vals = dict(base_vals)
            Company = env["res.company"]
            company = Company.browse(billing_company_id)
            company.flush_recordset()
            env.cr.execute(
                """UPDATE res_company
                      SET ai_usage_lock_version = ai_usage_lock_version + 1
                    WHERE id = %s""",
                [billing_company_id],
            )
            company.invalidate_recordset()
            budget_enforced = bool(company.ai_usage_enabled)
            attempt_vals["budget_enforced"] = budget_enforced
            AgentBudget = env["ai.agent.usage.budget"]
            budget = AgentBudget.search(
                [("company_id", "=", billing_company_id), ("agent_id", "=", agent_id)],
                limit=1,
            )
            if budget:
                budget.flush_recordset()
                env.cr.execute(
                    "SELECT id FROM ai_agent_usage_budget WHERE id = %s FOR UPDATE",
                    [budget.id],
                )
                budget.invalidate_recordset()
            Attempt = env["ai.agent.usage.attempt"]
            env.cr.execute(
                """SELECT
                       COALESCE(SUM(
                           CASE WHEN reservation_state = 'held'
                                THEN reservation_amount ELSE 0 END +
                           CASE WHEN reservation_state = 'settled'
                                THEN charged_amount ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN reservation_state = 'held'
                                THEN reservation_amount ELSE 0 END), 0)
                   FROM ai_agent_usage_attempt
                   WHERE company_id = %s AND budget_period_start = %s""",
                [billing_company_id, period],
            )
            company_used = float(env.cr.fetchone()[0])
            env.cr.execute(
                """SELECT COALESCE(SUM(
                       CASE WHEN reservation_state = 'held'
                            THEN reservation_amount ELSE 0 END +
                       CASE WHEN reservation_state = 'settled'
                            THEN charged_amount ELSE 0 END), 0)
                   FROM ai_agent_usage_attempt
                   WHERE company_id = %s AND agent_id = %s
                     AND budget_period_start = %s""",
                [billing_company_id, agent_id, period],
            )
            agent_used = float(env.cr.fetchone()[0])
            env.cr.execute(
                """SELECT COALESCE(SUM(
                       CASE WHEN reservation_state = 'held'
                            THEN reservation_amount ELSE 0 END +
                       CASE WHEN reservation_state = 'settled'
                            THEN charged_amount ELSE 0 END), 0)
                   FROM ai_agent_usage_attempt
                   WHERE run_db_id = %s""",
                [run_id],
            )
            run_used = float(env.cr.fetchone()[0])

            reason = False
            if budget_enforced and not rate_id:
                reason = "No rate card is configured for this provider and model."
            elif budget_enforced and not ceiling:
                reason = "No positive request ceiling is configured for this model."
            elif budget_enforced:
                if (
                    company.ai_usage_monthly_limit
                    and company_used + ceiling > company.ai_usage_monthly_limit
                ):
                    reason = "The company monthly AI budget is exhausted."
                elif (
                    budget
                    and budget.monthly_limit
                    and agent_used + ceiling > budget.monthly_limit
                ):
                    reason = "The agent monthly AI budget is exhausted."
                elif (
                    budget
                    and budget.run_limit
                    and run_used + ceiling > budget.run_limit
                ):
                    reason = "The run AI budget is exhausted."

            previous = Attempt.search(
                [("run_db_id", "=", run_id), ("sequence", "=", run.step_no)],
                order="ordinal desc",
                limit=1,
            )
            attempt_vals["ordinal"] = previous.ordinal + 1 if previous else 1
            if reason:
                attempt_vals.update(
                    state="blocked",
                    send_state="not_sent",
                    reservation_state="released",
                    reservation_amount=0.0,
                    request_ceiling=0.0,
                    error_summary=reason,
                )
            else:
                if not budget_enforced:
                    attempt_vals["reservation_state"] = "released"
                    attempt_vals["reservation_amount"] = 0.0
                    attempt_vals["request_ceiling"] = 0.0
                if not company.ai_usage_tracking_started_at:
                    env.cr.execute(
                        "UPDATE res_company SET ai_usage_tracking_started_at = %s "
                        "WHERE id = %s",
                        [attempt_vals["request_at"], billing_company_id],
                    )
                    company.invalidate_recordset(["ai_usage_tracking_started_at"])
            attempt = Attempt._new(attempt_vals)
            return dict(attempt_vals, id=attempt.id)

        created = self._durable(reserve)
        if config.get("test_enable"):
            return self.browse(created["id"]).sudo()
        return created

    @api.model
    def _set_attempt(self, attempt_id, values):
        def update(env):
            row = env["ai.agent.usage.attempt"].browse(attempt_id)
            if not row.exists():
                return False
            row.flush_recordset()
            env.cr.execute(
                "SELECT state FROM ai_agent_usage_attempt WHERE id = %s FOR UPDATE",
                [attempt_id],
            )
            state = env.cr.fetchone()[0]
            row.invalidate_recordset(["state"])
            if state in (
                "settled",
                "overage",
                "blocked",
                "failed",
                "adjusted",
                "released",
            ):
                return state
            row._set(values)
            return values.get("state") or state

        return self._durable(update)

    @api.model
    def _attempt_snapshot(self, attempt_id):
        """Read a ledger row from the transaction that owns its latest commit."""

        def read(env):
            row = env["ai.agent.usage.attempt"].sudo().browse(attempt_id)
            if not row.exists():
                return False
            return {
                "id": row.id,
                "attempt_uuid": row.attempt_uuid,
                "state": row.state,
                "send_state": row.send_state,
                "reservation_state": row.reservation_state,
                "reservation_amount": row.reservation_amount,
                "company_id": row.company_id.id,
                "company_currency_id": row.company_currency_id.id,
                "fx_rate": row.fx_rate,
                "fx_date": row.fx_date,
                "rate_id": row.rate_id.id,
                "decision_json": row.decision_json,
                "request_id": row.request_id,
                "charged_amount": row.charged_amount,
                "cost_status": row.cost_status,
                "cost_provenance": row.cost_provenance,
                "usage_status": row.usage_status,
                "usage_provenance": row.usage_provenance,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "provider_kind": row.provider_kind,
                "model": row.model,
                "error_summary": row.error_summary,
                "budgeted": row.budget_enforced,
            }

        return self._durable(read)

    @api.model
    def _estimate_input(self, provider, goal, transcript, schemas):
        if provider._effective_kind() == "anthropic":
            try:
                payload = provider._anthropic_payload(goal, transcript, schemas)
                endpoint = provider._endpoint().removesuffix("/messages")
                response = requests.post(
                    endpoint + "/messages/count_tokens",
                    headers={
                        "x-api-key": provider._api_key(),
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json=payload,
                    timeout=30,
                )
                if response.status_code == 200:
                    count = response.json().get("input_tokens")
                    if type(count) is int and count >= 0:
                        return count, "anthropic_count_tokens"
            except (requests.RequestException, ValueError, UserError) as exc:
                _logger.debug(
                    "Anthropic input-token estimation failed (%s).",
                    type(exc).__name__,
                )
            return None, "unavailable"
        if provider._effective_kind() == "openai":
            try:
                import tiktoken

                encoding = tiktoken.encoding_for_model(provider._effective_model())
                payload = provider._openai_payload(goal, transcript, schemas)
                content = json.dumps(payload, sort_keys=True, ensure_ascii=False)
                return len(encoding.encode(content)), "local_tiktoken_approximation"
            except (ImportError, KeyError, ValueError):
                return None, "unavailable"
        return None, "unavailable"

    @api.model
    def _estimate_output(self, provider, decision):
        if provider._effective_kind() != "openai":
            return None, "unavailable"
        try:
            import tiktoken

            encoding = tiktoken.encoding_for_model(provider._effective_model())
            return len(
                encoding.encode(json.dumps(decision, ensure_ascii=False))
            ), "local_tiktoken_approximation"
        except (ImportError, KeyError, ValueError):
            return None, "unavailable"

    @api.model
    def _normalize_usage(self, provider, result, goal, transcript, schemas):
        usage = result.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        reported = all(
            type(value) is int and value >= 0 for value in (input_tokens, output_tokens)
        )
        if reported:
            return input_tokens, output_tokens, "reported", "provider"
        sources = set()
        if type(input_tokens) is int and input_tokens >= 0:
            sources.add(result.get("usage_provenance") or "estimated")
        else:
            input_tokens, input_source = self._estimate_input(
                provider, goal, transcript, schemas
            )
            if input_tokens is not None:
                sources.add(input_source)
        if type(output_tokens) is int and output_tokens >= 0:
            sources.add("provider")
        else:
            output_tokens, output_source = self._estimate_output(
                provider, result.get("decision") or {}
            )
            if output_tokens is not None:
                sources.add(output_source)
        if input_tokens is None or output_tokens is None:
            return input_tokens, output_tokens, "unavailable", "unavailable"
        provenance = sources.pop() if len(sources) == 1 else "mixed_estimator"
        return input_tokens, output_tokens, "estimated", provenance

    @api.model
    def _price(self, rate, input_tokens, output_tokens, fx_rate, company_currency):
        if not rate or input_tokens is None or output_tokens is None:
            return None
        rate_amount = (
            rate.input_per_million * input_tokens
            + rate.output_per_million * output_tokens
        ) / 1_000_000
        return company_currency.round(rate_amount * fx_rate)

    @api.model
    def _settle(self, attempt, result, provider, goal, transcript, schemas, rate):
        attempt_id = attempt["id"] if isinstance(attempt, dict) else attempt.id
        snapshot = self._attempt_snapshot(attempt_id)
        if snapshot["state"] in ("settled", "overage", "adjusted", "released"):
            if snapshot["decision_json"]:
                result["decision"] = json.loads(snapshot["decision_json"])
            result.update(
                {
                    "request_id": snapshot["request_id"],
                    "cost": snapshot["charged_amount"]
                    if snapshot["cost_status"] != "unpriced"
                    else None,
                    "cost_status": snapshot["cost_status"],
                    "cost_provenance": snapshot["cost_provenance"],
                    "attempt_uuid": snapshot["attempt_uuid"],
                }
            )
            return result, "overage" if snapshot["state"] == "overage" else "settled"
        input_tokens, output_tokens, usage_status, usage_source = self._normalize_usage(
            provider, result, goal, transcript, schemas
        )
        company_currency = self.env["res.currency"].browse(
            snapshot["company_currency_id"]
        )
        company = self.env["res.company"].browse(snapshot["company_id"])
        reported_cost = result.get("cost")
        reported_amount = None
        reported_currency = self.env["res.currency"]
        reported_fx_rate = 0.0
        actual = None
        if isinstance(reported_cost, dict):
            amount = reported_cost.get("amount")
            currency_code = reported_cost.get("currency")
            if (
                type(amount) in (int, float)
                and amount >= 0
                and isinstance(currency_code, str)
            ):
                reported_currency = (
                    self.env["res.currency"]
                    .sudo()
                    .search([("name", "=", currency_code)], limit=1)
                )
                if reported_currency:
                    reported_amount = amount
                    actual, reported_fx_rate = self._convert(
                        amount,
                        reported_currency,
                        company_currency,
                        company.sudo(),
                        snapshot["fx_date"],
                    )
        cost_status = "reported" if actual is not None else "unpriced"
        cost_provenance = "provider" if actual is not None else "unpriced"
        if actual is None:
            actual = self._price(
                rate,
                input_tokens,
                output_tokens,
                snapshot["fx_rate"],
                company_currency,
            )
            cost_status = "estimated" if actual is not None else "unpriced"
            cost_provenance = "rate_card" if actual is not None else "unpriced"
        decision_json = json.dumps(
            result.get("decision") or {}, ensure_ascii=False, separators=(",", ":")
        )
        result.update(
            {
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
                "usage_status": usage_status,
                "usage_provenance": usage_source,
                "cost": actual,
                "cost_currency": company_currency.name if actual is not None else None,
                "cost_status": cost_status,
                "cost_provenance": cost_provenance,
            }
        )
        if actual is None and snapshot["budgeted"]:
            self._set_attempt(
                attempt_id,
                {
                    "state": "unknown",
                    "send_state": "received",
                    "reservation_state": "held",
                    "usage_status": usage_status,
                    "usage_provenance": usage_source,
                    "input_tokens": input_tokens or 0,
                    "output_tokens": output_tokens or 0,
                    "cost_status": "unpriced",
                    "cost_provenance": "unpriced",
                    "provider_cost_currency_id": False,
                    "provider_cost_amount": 0.0,
                    "provider_cost_fx_rate": 0.0,
                    "request_id": result.get("request_id") or False,
                    "decision_json": decision_json,
                    "settled_at": fields.Datetime.now(),
                    "error_summary": (
                        "Provider response received but usage or cost could not be "
                        "priced."
                    ),
                },
            )
            return result, "hold"
        actual = actual or 0.0
        overage = bool(
            snapshot["reservation_amount"] and actual > snapshot["reservation_amount"]
        )
        self._set_attempt(
            attempt_id,
            {
                "state": "overage" if overage else "settled",
                "send_state": "received",
                "reservation_state": "settled" if actual else "released",
                "usage_status": usage_status,
                "usage_provenance": usage_source,
                "input_tokens": input_tokens or 0,
                "output_tokens": output_tokens or 0,
                "cost_status": cost_status,
                "cost_provenance": cost_provenance,
                "provider_cost_currency_id": reported_currency.id
                if reported_amount is not None
                else False,
                "provider_cost_amount": reported_amount or 0.0,
                "provider_cost_fx_rate": reported_fx_rate,
                "request_id": result.get("request_id") or False,
                "charged_amount": actual,
                "decision_json": decision_json,
                "settled_at": fields.Datetime.now(),
                "error_summary": (
                    "Actual estimated cost exceeded the reserved request ceiling."
                )
                if overage
                else False,
            },
        )
        return result, "overage" if overage else "settled"

    @api.model
    def _decide_metered(self, run, provider, goal, transcript, schemas):
        """Reserve, send once, then settle in separately committed transactions."""
        company = run.billing_company_id
        period = self._company_month(company)
        rate = self._find_rate(provider)
        self.invalidate_model(
            ["state", "reservation_state", "decision_json", "error_summary"]
        )
        existing = self.sudo().search(
            [("run_db_id", "=", run.id), ("sequence", "=", run.step_no)],
            order="ordinal desc",
            limit=1,
        )
        if (
            existing
            and existing.decision_json
            and existing.state
            in (
                "settled",
                "overage",
                "adjusted",
                "released",
            )
        ):
            result = {
                "decision": json.loads(existing.decision_json),
                "provider_kind": existing.provider_kind,
                "model": existing.model,
                "request_id": existing.request_id,
                "usage": {
                    "input_tokens": existing.input_tokens,
                    "output_tokens": existing.output_tokens,
                },
                "usage_status": existing.usage_status,
                "usage_provenance": existing.usage_provenance,
                "cost": existing.charged_amount
                if existing.cost_status != "unpriced"
                else None,
                "cost_status": existing.cost_status,
                "cost_provenance": existing.cost_provenance,
                "attempt_uuid": existing.attempt_uuid,
                "usage_overage": existing.state == "overage",
            }
            return result
        if existing and existing.state in ("sending", "unknown"):
            return {
                "usage_hold_reason": existing.error_summary
                or (
                    "Provider send started; its outcome must be resolved before "
                    "retrying."
                )
            }
        if existing and existing.state == "reserved":
            attempt = self._attempt_snapshot(existing.id)
            rate = self.env["ai.agent.usage.rate"].browse(attempt["rate_id"])
        else:
            attempt = self._reserve(run, provider, rate, period)
            if not isinstance(attempt, dict):
                attempt = self._attempt_snapshot(attempt.id)
        if attempt["state"] == "blocked":
            return {"usage_hold_reason": attempt.get("error_summary")}
        # Re-use a reservation after a crash only if the durable state proves
        # that no send was started. Sending is marked before entering requests.
        if attempt["state"] != "reserved":
            return {
                "usage_hold_reason": attempt.get("error_summary")
                or "Provider attempt is held for administrator resolution."
            }
        attempt_id = attempt["id"]

        def before_send():
            self._set_attempt(
                attempt_id,
                {
                    "state": "sending",
                    "send_state": "sending",
                    "sent_at": fields.Datetime.now(),
                },
            )

        if provider._effective_kind() == "stub":
            before_send()
        try:
            result = provider._decide_result(
                goal, transcript, schemas, on_send=before_send
            )
        except Exception as exc:
            latest = self._attempt_snapshot(attempt_id)
            if latest["send_state"] == "not_sent":
                self._set_attempt(
                    attempt_id,
                    {
                        "state": "failed",
                        "reservation_state": "released",
                        "error_summary": f"{type(exc).__name__}: {exc}"[:250],
                    },
                )
                raise
            self._set_attempt(
                attempt_id,
                {
                    "state": "unknown",
                    "send_state": "sending",
                    "reservation_state": "held",
                    "error_summary": (
                        "Provider send started; no durable response was recorded."
                    ),
                },
            )
            return {
                "usage_hold_reason": (
                    "Provider send started; no durable response was recorded."
                )
            }
        result["attempt_uuid"] = attempt["attempt_uuid"]
        settled, status = self._settle(
            attempt, result, provider, goal, transcript, schemas, rate
        )
        if status == "hold":
            return {
                "usage_hold_reason": (
                    "Provider response received but usage or cost could not be priced."
                )
            }
        if status == "overage":
            settled["usage_overage"] = True
        return settled

    def action_adjust_hold(self):
        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(
                self.env._("Only administrators can resolve usage holds.")
            )
        if self.state not in ("sending", "unknown") or self.reservation_state != "held":
            raise UserError(self.env._("This attempt has no unresolved reservation."))
        run = self.env["ai.agent.run"].browse(self.run_db_id)
        if not run.exists() or run.state != "usage_hold":
            raise UserError(
                self.env._("Pause the run before resolving its provider reservation.")
            )
        if not self.resolution_note_input or not self.resolution_note_input.strip():
            raise UserError(
                self.env._("Enter an audit note before adjusting the hold.")
            )
        if self.resolution_amount_input < 0:
            raise UserError(self.env._("The adjusted amount cannot be negative."))
        overage = self.resolution_amount_input > self.request_ceiling
        self._set(
            {
                "state": "overage" if overage else "adjusted",
                "reservation_state": "settled",
                "charged_amount": self.resolution_amount_input,
                "cost_status": "adjusted",
                "cost_provenance": "admin_adjustment",
                "resolution_action": "adjust",
                "resolution_note": self.resolution_note_input.strip(),
                "resolved_by": self.env.uid,
                "resolved_at": fields.Datetime.now(),
                "settled_at": fields.Datetime.now(),
                "error_summary": "Administrator adjustment exceeds the request ceiling."
                if overage
                else False,
            }
        )
        return True

    def action_release_hold(self):
        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(
                self.env._("Only administrators can resolve usage holds.")
            )
        if self.state not in ("sending", "unknown") or self.reservation_state != "held":
            raise UserError(self.env._("This attempt has no unresolved reservation."))
        run = self.env["ai.agent.run"].browse(self.run_db_id)
        if not run.exists() or run.state != "usage_hold":
            raise UserError(
                self.env._("Pause the run before resolving its provider reservation.")
            )
        if not self.resolution_note_input or not self.resolution_note_input.strip():
            raise UserError(
                self.env._("Enter an audit note before releasing the hold.")
            )
        self._set(
            {
                "state": "released",
                "reservation_state": "released",
                "charged_amount": 0.0,
                "resolution_action": "release",
                "resolution_note": self.resolution_note_input.strip(),
                "resolved_by": self.env.uid,
                "resolved_at": fields.Datetime.now(),
                "settled_at": fields.Datetime.now(),
                "error_summary": False,
            }
        )
        return True
