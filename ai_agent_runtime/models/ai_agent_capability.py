# pylint: disable=method-required-super,no-raise-unlink,context-overridden
# Only private server methods may mutate authority records.
from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

LEVELS = [(level, level.title()) for level in ("observe", "propose", "apply")]


class AiAgentCapability(models.Model):
    _name = "ai.agent.capability"
    _description = "AI Agent Capability"
    _rec_name = "tool_id"

    agent_id = fields.Many2one("ai.agent", required=True, ondelete="restrict")
    tool_id = fields.Many2one("ai.agent.tool", required=True, ondelete="restrict")
    level = fields.Selection(LEVELS, default="observe", required=True)
    granted = fields.Boolean(default=True)
    promotion_locked = fields.Boolean(default=False)
    epoch = fields.Integer(default=1, required=True)
    epoch_started = fields.Datetime(default=fields.Datetime.now, required=True)
    policy = fields.Text(required=True)
    reason = fields.Text()
    history = fields.Json(default=list, readonly=True)
    evidence_ids = fields.One2many("ai.agent.evidence", "capability_id", readonly=True)

    _unique_grant = models.Constraint(
        "UNIQUE(agent_id, tool_id)", "A capability already exists for this grant."
    )

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(
            self.env._("Capabilities are synchronized from agent grants.")
        )

    def write(self, vals):
        raise AccessError(self.env._("Use a reasoned authority transition."))

    def unlink(self):
        raise AccessError(self.env._("Keep revoked capabilities for attribution."))

    @api.model
    def _new(self, vals):
        return super(AiAgentCapability, self.sudo().with_context({})).create(vals)

    def _set(self, vals):
        return super(AiAgentCapability, self.sudo()).write(vals)

    def _lock(self):
        if not self:
            return
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM ai_agent_capability WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(self.ids)],
        )
        self.invalidate_recordset()

    def _change(self, reason, **vals):
        self._lock()
        for cap in self:
            stats = cap._statistics()
            event = {
                "previous_level": cap.level,
                "new_level": vals.get("level", cap.level),
                "epoch": cap.epoch,
                "at": fields.Datetime.to_string(fields.Datetime.now()),
                "actor_id": self.env.uid,
                "reason": reason,
                **stats,
            }
            cap._set(
                {
                    "epoch": cap.epoch + 1,
                    "epoch_started": fields.Datetime.now(),
                    "policy": cap.tool_id._policy_signature(),
                    "reason": reason,
                    "history": [*(cap.history or []), event],
                    **vals,
                }
            )
            if (
                event["previous_level"] != event["new_level"]
                or "promotion_locked" in vals
            ):
                cap._notify_transition(event)

    def _statistics(self):
        self.ensure_one()
        evidence = (
            self.env["ai.agent.evidence"]
            .sudo()
            .search(
                [
                    ("capability_id", "=", self.id),
                    ("epoch", "=", self.epoch),
                ],
                order="id",
            )
        )
        judgments = evidence.filtered(
            lambda e: (
                e.kind == "judgment"
                and (
                    e.verdict == "reject"
                    or e.call_id.state in ("observed", "dispatched", "undone")
                )
            )
        )
        approvals = len(judgments.filtered(lambda e: e.verdict == "accept"))
        streak = 0
        for item in reversed(judgments):
            if item.verdict != "reject":
                break
            streak += 1
        return {
            "approvals": approvals,
            "rejections": len(judgments) - approvals,
            "undos": len(evidence.filtered(lambda e: e.kind == "undo")),
            "negative_streak": streak,
            "period_start": fields.Datetime.to_string(judgments[:1].judged_at)
            if judgments
            else False,
            "period_end": fields.Datetime.to_string(judgments[-1:].judged_at)
            if judgments
            else False,
        }

    def _notify_transition(self, event):
        recipients = self.agent_id.reviewer_ids.filtered(
            lambda u: u.active and u.has_group("ai_agent_runtime.group_ai_reviewer")
        )
        recipients -= (
            self.env["ai.agent"]
            .sudo()
            .with_context(active_test=False)
            .search([("user_id", "in", recipients.ids)])
            .user_id
        )
        if recipients:
            self.env["mail.thread"].sudo().with_context(
                mail_notify_force_send=False
            ).message_notify(
                model=self._name,
                res_id=self.id,
                partner_ids=recipients.partner_id.ids,
                subject=self.env._("AI capability authority changed"),
                body=str(event),
            )

    def _lock_authority(self):
        self.ensure_one()
        self.agent_id.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM ai_agent WHERE id = %s FOR UPDATE", [self.agent_id.id]
        )
        self.agent_id.invalidate_recordset()
        self.tool_id.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM ai_agent_tool WHERE id = %s FOR UPDATE", [self.tool_id.id]
        )
        self.tool_id.invalidate_recordset()
        self._lock()

    def _check_demotion(self):
        self._lock()
        for cap in self:
            if cap._statistics()["negative_streak"] >= cap.agent_id.rejection_limit:
                level = "propose" if cap.level == "apply" else "observe"
                cap._change("Consecutive reviewer rejections.", level=level)

    def action_veto(self, reason):
        if not reason or not reason.strip():
            raise UserError(self.env._("A veto reason is required."))
        for cap in self:
            cap.check_access("read")
            cap._lock_authority()
            cap.agent_id._check_reviewer()
            cap._change(reason, level="observe", promotion_locked=True)
        return True

    def _evaluate_promotion(self):
        self.ensure_one()
        self._lock_authority()
        self._refresh_policy()
        if (
            not self.granted
            or self.tool_id not in self.agent_id.tool_ids
            or not self.agent_id.active
            or self.promotion_locked
            or self.level == "apply"
        ):
            return False
        stats = self._statistics()
        samples = stats["approvals"] + stats["rejections"]
        if (
            samples < self.agent_id.promotion_samples
            or 100 * stats["approvals"] < self.agent_id.promotion_rate * samples
        ):
            return False
        level = "propose" if self.level == "observe" else "apply"
        if level == "apply":
            if (
                self.tool_id.classification == "irreversible"
                and self.agent_id.irreversible_guard
            ):
                return False
            if (
                self.tool_id.classification != "readonly"
                and not self.tool_id._undo_adapter()
            ):
                return False
        self._change(
            "Automatic promotion from qualifying reviewer evidence.", level=level
        )
        return True

    @api.model
    def _cron_promote(self):
        # Private entry point, always invoked by the server-owned cron identity.
        for cap in self.sudo().search([("granted", "=", True)]):
            cap._evaluate_promotion()
        return True

    def _refresh_policy(self):
        self._lock()
        for cap in self:
            if cap.policy != cap.tool_id._policy_signature():
                cap._change("Tool or reversal contract changed.", level="observe")

    def action_set_level(self, level, reason):
        self.env["ai.agent"]._check_admin()
        if level not in dict(LEVELS) or not reason or not reason.strip():
            raise UserError(self.env._("A valid level and a reason are required."))
        for cap in self:
            cap._lock_authority()
            cap._change(reason, level=level)
        return True

    def action_clear_lock(self, reason):
        self.env["ai.agent"]._check_admin()
        if not reason or not reason.strip():
            raise UserError(self.env._("A reason is required."))
        for cap in self:
            cap._lock_authority()
            cap._change(reason, promotion_locked=False)
        return True

    def action_open_manual(self):
        self.ensure_one()
        self.env["ai.agent"]._check_admin()
        return self.env["ai.agent.authority.wizard"]._open("manual", capability=self)

    def action_open_veto(self):
        self.ensure_one()
        self.agent_id._check_reviewer()
        return self.env["ai.agent.authority.wizard"]._open("veto", capability=self)

    def action_open_unlock(self):
        self.ensure_one()
        self.env["ai.agent"]._check_admin()
        return self.env["ai.agent.authority.wizard"]._open("unlock", capability=self)
