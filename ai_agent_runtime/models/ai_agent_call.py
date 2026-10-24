# pylint: disable=method-required-super,no-raise-unlink,context-overridden
# Protected audit records deliberately reject public mutation and deletion.
import json

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError


class AiAgentCall(models.Model):
    """Immutable invocation inputs; lifecycle changes belong to the dispatcher."""

    _name = "ai.agent.call"
    _description = "AI Agent Invocation"
    _order = "run_id, sequence"

    run_id = fields.Many2one("ai.agent.run", required=True, ondelete="restrict")
    agent_id = fields.Many2one(related="run_id.agent_id", store=True)
    sequence = fields.Integer(required=True)
    tool_id = fields.Many2one("ai.agent.tool", ondelete="restrict")
    tool_name = fields.Char(required=True)
    model_name = fields.Char()
    method_name = fields.Char()
    policy = fields.Text()
    args = fields.Text(required=True)
    user_id = fields.Many2one("res.users", required=True, ondelete="restrict")
    execution_context = fields.Json(required=True)
    state = fields.Selection(
        [
            (s, s.title())
            for s in (
                "pending",
                "approved",
                "observed",
                "refused",
                "failed",
                "dispatched",
                "undone",
            )
        ],
        required=True,
        default="pending",
    )
    approver_id = fields.Many2one("res.users", ondelete="restrict")
    result = fields.Text()
    receipt = fields.Json()
    undo_state = fields.Selection(
        [(s, s.title()) for s in ("none", "requested", "failed", "done")],
        default="none",
    )
    undo_error = fields.Text()
    capability_id = fields.Many2one("ai.agent.capability", ondelete="restrict")
    capability_epoch = fields.Integer()
    capability_level = fields.Selection(
        [(level, level.title()) for level in ("observe", "propose", "apply")]
    )
    _unique_call = models.Constraint(
        "UNIQUE(run_id, sequence)", "An invocation already exists at this sequence."
    )

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(self.env._("Only the dispatcher can create invocations."))

    def write(self, vals):
        raise AccessError(self.env._("Invocation snapshots are protected."))

    def unlink(self):
        raise AccessError(self.env._("Invocation history cannot be deleted."))

    @api.model
    def _new(self, vals):
        return super(AiAgentCall, self.sudo().with_context({})).create(vals)

    def _set(self, vals):
        return super(AiAgentCall, self.sudo()).write(vals)

    def _validate(self):
        self.ensure_one()
        run, tool = self.run_id, self.tool_id
        if not tool:
            raise AccessError(self.env._("Agent is not allowed to use this tool."))
        record = self._execution_record()
        if not self.env["ai.agent.surface"]._is_callable_remotely(
            record, self.method_name
        ):
            raise AccessError(self.env._("The target is no longer remotely callable."))
        cap = self.capability_id
        cap._refresh_policy()
        if not cap or not cap.granted or cap.epoch != self.capability_epoch:
            raise AccessError(self.env._("Capability changed; start a new invocation."))
        if (
            not tool
            or tool not in run.agent_id.tool_ids
            or not run.agent_id.active
            or run.agent_id.user_id != self.user_id
            or run.execution_user_id != self.user_id
            or not self.user_id.active
            or not set(self.execution_context["allowed_company_ids"]).issubset(
                self.user_id.company_ids.ids
            )
            or self.policy != tool._policy_signature()
        ):
            raise AccessError(
                self.env._("Invocation authority changed; start a new run.")
            )

    def _arguments(self):
        self.ensure_one()
        return json.loads(self.args)

    evidence_ids = fields.One2many("ai.agent.evidence", "call_id", readonly=True)

    def _review_lock(self):
        self.ensure_one()
        self.check_access("read")
        self.run_id._lock()
        self.run_id._lock_authority(self.tool_id)
        self.capability_id._lock()
        self.invalidate_recordset()
        self.agent_id._check_reviewer()

    def _judge(self, verdict, reason):
        self._review_lock()
        if self.state not in ("observed", "pending") or self.evidence_ids.filtered(
            lambda e: e.kind == "judgment"
        ):
            raise UserError(self.env._("This call cannot be judged again."))
        self.env["ai.agent.evidence"]._record(self, "judgment", verdict, reason)
        self._set({"approver_id": self.env.uid})
        if self.state == "pending":
            if verdict == "accept":
                self._set({"state": "approved"})
                self.run_id._set({"state": "running"})
            else:
                self._set({"state": "refused", "result": "Rejected by reviewer."})
                self.run_id._finish_call(self)
                self.run_id._set(
                    {"state": "cancelled", "error": "Rejected by reviewer."}
                )
        self.capability_id._check_demotion()
        return True

    def action_accept(self, reason=""):
        for call in self:
            call._judge("accept", reason)
        return True

    def action_reject(self, reason=""):
        for call in self:
            call._judge("reject", reason)
        return True

    def _execution_record(self):
        # pylint: disable=context-overridden
        return (
            self.env[self.model_name]
            .with_context(dict(self.execution_context))
            .with_user(self.user_id)
        )

    def action_undo(self, reason):
        if not reason or not reason.strip():
            raise UserError(self.env._("An undo reason is required."))
        for call in self:
            call._review_lock()
            if call.state != "dispatched" or call.undo_state != "none":
                raise UserError(self.env._("Only an applied call can be undone once."))
            self.env["ai.agent.evidence"]._record(call, "undo", "request", reason)
            cap = call.capability_id
            # The downgrade must survive a failed reversal savepoint.
            cap._change(
                "Undo requested: " + reason,
                level="propose" if cap.level == "apply" else cap.level,
            )
            call._set({"undo_state": "requested"})
            try:
                with self.env.cr.savepoint():
                    adapter = call.tool_id._undo_adapter()
                    if (
                        not adapter
                        or call.policy != call.tool_id._policy_signature()
                        or call.receipt is False
                    ):
                        raise UserError(
                            self.env._(
                                "No matching reversal receipt; manual repair required."
                            )
                        )
                    adapter.reverse(call._execution_record(), call.receipt)
            except Exception as exc:  # noqa: BLE001 - preserve demotion and applied state
                call._set({"undo_state": "failed", "undo_error": str(exc)[:2000]})
            else:
                call._set(
                    {"state": "undone", "undo_state": "done", "undo_error": False}
                )
        return True
