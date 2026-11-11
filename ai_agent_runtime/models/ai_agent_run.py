# pylint: disable=method-required-super,no-raise-unlink,context-overridden
# Protected audit records deliberately reject public mutation and deletion.
import inspect
import json
import logging

from psycopg2 import errors as pg_errors

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from ..adapters import invocation_scope

_logger = logging.getLogger(__name__)


class AiAgentRun(models.Model):
    _name = "ai.agent.run"
    _description = "AI Agent Run"
    _order = "id desc"

    name = fields.Char(compute="_compute_name")
    agent_id = fields.Many2one("ai.agent", required=True, ondelete="restrict")
    state = fields.Selection(
        [
            (s, s.replace("_", " ").title())
            for s in (
                "draft",
                "running",
                "awaiting_approval",
                "done",
                "failed",
                "cancelled",
            )
        ],
        default="draft",
        required=True,
        readonly=True,
    )
    plan = fields.Text(required=True, default="[]")
    company_ids = fields.Many2many(
        "res.company",
        string="Execution companies",
        help="Empty uses the execution user's default company. Frozen at start.",
    )
    step_no = fields.Integer(default=0, readonly=True)
    step_ids = fields.One2many("ai.agent.run.step", "run_id", readonly=True)
    call_ids = fields.One2many("ai.agent.call", "run_id", readonly=True)
    pending_call_id = fields.Many2one(
        "ai.agent.call", readonly=True, ondelete="restrict"
    )
    pending_tool_id = fields.Many2one(related="pending_call_id.tool_id")
    pending_args = fields.Text(related="pending_call_id.args")
    approver_id = fields.Many2one(related="pending_call_id.approver_id")
    execution_user_id = fields.Many2one("res.users", readonly=True, ondelete="restrict")
    execution_context = fields.Json(readonly=True)
    error = fields.Text(readonly=True)

    @api.depends("agent_id", "state")
    def _compute_name(self):
        for run in self:
            run.name = f"{run.agent_id.name or '?'} #{run.id or 0}"

    @api.model
    def _control_fields(self):
        return {
            "state",
            "step_no",
            "step_ids",
            "call_ids",
            "pending_call_id",
            "pending_tool_id",
            "pending_args",
            "approver_id",
            "execution_user_id",
            "execution_context",
            "error",
        }

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if self._control_fields().intersection(vals) or any(
                "default_" + field in self.env.context
                for field in self._control_fields()
            ):
                raise AccessError(self.env._("Run control fields are server-owned."))
        return super().create(vals_list)

    def write(self, vals):
        if self._control_fields().intersection(vals):
            raise AccessError(self.env._("Run control fields are server-owned."))
        self._lock()
        if any(run.state != "draft" for run in self):
            raise AccessError(self.env._("Started runs are immutable."))
        return super().write(vals)

    def unlink(self):
        if any(run.state != "draft" for run in self):
            raise AccessError(self.env._("Started runs cannot be deleted."))
        return super().unlink()

    def _set(self, vals):
        return super(AiAgentRun, self.sudo()).write(vals)

    def _lock(self):
        if not self:
            return
        self.check_access("write")
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM ai_agent_run WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(self.ids)],
        )
        self.invalidate_recordset()

    def _plan_entries(self):
        self.ensure_one()
        try:
            entries = json.loads(self.plan or "[]")
        except ValueError as exc:
            raise UserError(
                self.env._("The plan is not valid JSON: %(err)s", err=exc)
            ) from exc
        if not isinstance(entries, list):
            raise UserError(self.env._("The plan must be a JSON list of steps."))
        return entries

    def _resolve_tool(self, name):
        tool = self.agent_id._tool_by_name(name)
        if not tool:
            raise AccessError(
                self.env._("Agent is not allowed to use tool %(tool)s.", tool=name)
            )
        return tool

    def _annotate_result(self, tool, args, result):
        return repr(result)

    # A fresh context is required: caller flags must not reach the target.
    # pylint: disable=context-overridden
    def _invoke(self, call):
        """Execute only the stored snapshot, with a clean, frozen context."""
        record = call._execution_record()
        if not self.env["ai.agent.surface"]._is_callable_remotely(
            record, call.method_name
        ):
            raise AccessError(self.env._("The target is no longer remotely callable."))
        bound = getattr(record, call.method_name)
        args = call._arguments()
        try:
            inspect.signature(bound).bind(**args)
        except TypeError as exc:
            raise UserError(
                self.env._("Tool arguments do not match: %(err)s", err=str(exc))
            ) from exc
        return bound(**args)

    def _snapshot_values(self, tool, name, args):
        cap = self.agent_id._capability(tool)
        cap._refresh_policy()
        return {
            "capability_id": cap.id,
            "capability_epoch": cap.epoch,
            "capability_level": cap.level,
            "run_id": self.id,
            "sequence": self.step_no,
            "tool_id": tool.id,
            "tool_name": name or "(unknown)",
            "model_name": tool.model_name,
            "method_name": tool.method_name,
            "policy": tool._policy_signature() if tool else False,
            "args": json.dumps(args),
            "user_id": self.execution_user_id.id,
            "execution_context": self.execution_context,
        }

    def _lock_authority(self, tool):
        """Pin the authority configuration for the duration of a dispatch.

        Share mode, not exclusive. The invariant is reader/writer: a dispatch
        *reads* the agent's grants and the tool's policy signature and writes
        neither row, while every path that *changes* authority already takes
        ``FOR UPDATE`` on them (``ai_agent.write``,
        ``ai_agent_tool._lock``, ``ai_agent_capability._lock``). ``FOR SHARE``
        keeps that mutual exclusion intact in both directions and drops only the
        dispatcher-against-dispatcher conflict.

        Exclusive mode made the tool row a global bottleneck: a tool is shared by
        every agent that uses it, so any two runs invoking the same tool
        serialised regardless of which agents owned them, and a shared tool
        catalogue serialised on its busiest entry.
        """
        self.agent_id.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM ai_agent WHERE id = %s FOR SHARE", [self.agent_id.id]
        )
        self.agent_id.invalidate_recordset()
        if tool:
            tool.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM ai_agent_tool WHERE id = %s FOR SHARE", [tool.id]
            )
            tool.invalidate_recordset()

    def _finish_call(self, call, recover=False):
        self.env["ai.agent.run.step"]._new(
            {
                "run_id": self.id,
                "sequence": call.sequence,
                "call_id": call.id,
                "tool_id": call.tool_id.id,
                "args": call.args,
                "result": call.result,
                "failed": call.state in ("failed", "refused"),
                "dispatch_state": call.state,
            }
        )
        vals = {"step_no": self.step_no + 1, "pending_call_id": False}
        if call.state == "refused" or (call.state == "failed" and not recover):
            vals.update(state="failed", error=call.result)
        self._set(vals)
        return self.state == "running"

    def _dispatch(self, name=None, args=None, recover=False):
        """The only boundary shared by static, model-selected and approved calls."""
        self.ensure_one()
        call = self.pending_call_id
        tool = call.tool_id if call else self.agent_id._tool_by_name(name)
        self._lock_authority(tool)
        if not call:
            call = self.env["ai.agent.call"]._new(
                self._snapshot_values(tool, name, args or {})
            )
        try:
            call._validate()
        except AccessError as exc:
            call._set({"state": "refused", "result": str(exc)})
            return self._finish_call(call)
        if tool.classification != "readonly" and call.capability_level == "observe":
            call._set(
                {"state": "observed", "result": "Intent recorded; target not invoked."}
            )
            return self._finish_call(call, recover=recover)
        needs_approval = tool.requires_approval or (
            tool.classification != "readonly" and call.capability_level == "propose"
        )
        if needs_approval and call.state != "approved":
            self._set({"state": "awaiting_approval", "pending_call_id": call.id})
            return False
        try:
            with self.env.cr.savepoint(), invocation_scope(call):
                adapter = tool._undo_adapter()
                prepared = (
                    adapter.prepare(call._execution_record(), call._arguments())
                    if adapter
                    else None
                )
                result = self._invoke(call)
                if adapter:
                    receipt = adapter.capture(
                        call._execution_record(), prepared, result
                    )
                    call._set({"receipt": json.loads(json.dumps(receipt))})
                rendered = self._annotate_result(tool, call._arguments(), result)[:2000]
        except (pg_errors.SerializationFailure, pg_errors.DeadlockDetected):
            # Let Odoo retry the transaction. Recording this as a tool failure
            # would commit an outcome from a transaction whose snapshot lost.
            raise
        except Exception as exc:  # noqa: BLE001 - recoverable tool errors
            call._set(
                {"state": "failed", "result": f"{type(exc).__name__}: {exc}"[:2000]}
            )
        else:
            call._set({"state": "dispatched", "result": rendered})
        return self._finish_call(call, recover=recover)

    def _advance(self):
        self.ensure_one()
        self._lock()
        if self.state != "running":
            return False
        entries = self._plan_entries()
        if self.step_no >= len(entries):
            self._set({"state": "done"})
            return False
        entry = entries[self.step_no]
        more = self._dispatch(entry.get("tool"), entry.get("args") or {})
        if more and self.step_no >= len(entries):
            self._set({"state": "done"})
            return False
        return more

    def _start_values(self):
        user = self.agent_id.user_id
        companies = self.company_ids or user.company_id
        if not set(companies.ids).issubset(user.company_ids.ids):
            raise AccessError(
                self.env._("Execution companies exceed the agent user's access.")
            )
        return {
            "state": "running",
            "execution_user_id": user.id,
            "execution_context": {
                "allowed_company_ids": companies.ids,
                "lang": user.lang,
                "tz": user.tz,
            },
        }

    def action_start(self):
        self._lock()
        for run in self:
            if run.state != "draft":
                raise UserError(self.env._("Only draft runs can be started."))
            run._set(run._start_values())
        return True

    def action_approve(self):
        self._lock()
        for run in self:
            if run.state != "awaiting_approval":
                raise UserError(self.env._("This run is not awaiting approval."))
            run.pending_call_id.action_accept()
        return True

    def action_reject(self):
        self._lock()
        for run in self:
            if run.state != "awaiting_approval":
                raise UserError(self.env._("This run is not awaiting approval."))
            run.pending_call_id.action_reject()
        return True

    def action_run_to_completion(self):
        for run in self:
            while run._advance():
                pass
        return True

    @api.model
    def _claim_next(self, exclude):
        """Claim the oldest running run no other drain lane is holding.

        ``SKIP LOCKED`` is deliberately scoped to this claim and nowhere else.
        ``_lock`` stays blocking on the approval and write paths, which want
        *this* record and must wait for whoever holds it; claiming wants the
        *next available* one and must not queue.

        No lease column is needed. ``state`` is already the durable source of
        truth and a claim covers exactly one step, so a worker killed mid-step
        rolls that step back and the run is simply claimable again.
        """
        self.env.cr.execute(
            """
            SELECT id
              FROM ai_agent_run
             WHERE state = 'running'
               AND id <> ALL(%s)
             ORDER BY id
               FOR UPDATE SKIP LOCKED
             LIMIT 1
            """,
            [list(exclude) or [0]],
        )
        row = self.env.cr.fetchone()
        return self.browse(row[0]) if row else None

    @api.model
    def _cron_drain(self):
        """Advance running runs one step at a time, oldest first.

        One step per claim rather than one run to completion. Running a whole
        plan per claim means a second conversation cannot take a single step
        until the first has finished: with eight 4-step runs that measured a
        140.7s wait for the last run's first step, and duplicating the cron row
        into four lanes changed nothing, because every lane selected the same
        newest-first list and queued on the same blocking ``FOR UPDATE``.
        Ordering is ``id`` ascending for the same reason -- the model's
        ``_order`` is ``id desc``, which serves newest-first and starves the
        oldest run under sustained arrivals.
        """
        Cron = self.env["ir.cron"]
        serviced = []
        while True:
            outstanding = self.search_count([("state", "=", "running")])
            if not outstanding:
                Cron._commit_progress(remaining=0)
                return True

            run = self._claim_next(serviced)
            if run is None:
                # Everything outstanding is either held by a peer lane or
                # already serviced this pass. Report the work as still
                # remaining so the job is rescheduled rather than reported
                # complete, then start a fresh pass.
                if not serviced:
                    Cron._commit_progress(processed=0, remaining=outstanding)
                    return True
                serviced = []
                continue

            serviced.append(run.id)
            try:
                with self.env.cr.savepoint():
                    run._advance()
            except (pg_errors.SerializationFailure, pg_errors.DeadlockDetected):
                # Not a tool failure. `_dispatch` re-raises these deliberately
                # because the losing transaction's snapshot is void -- but
                # `ir_cron._callback` has NO retry (it rolls back and re-raises;
                # the retry lives in the HTTP dispatcher only), so swallowing
                # them here would record a concurrency artifact as a business
                # failure, indistinguishable from a real one in the data. Let it
                # reach the cron, which rolls back this uncommitted step; the run
                # stays `running` and is claimed again next pass.
                raise
            except Exception:
                _logger.exception("ai.agent.run %s drain failed", run.id)
                run._set({"state": "failed", "error": "Unhandled error; see log."})

            # Commits, which releases the claimed row and lets a peer lane take
            # it. `remaining` is recomputed from live state rather than
            # decremented: if it ever reached 0 with work outstanding, core would
            # match `case (True, _, 0)` -> FULLY_DONE and park the lane.
            outstanding = self.search_count([("state", "=", "running")])
            if not Cron._commit_progress(processed=1, remaining=outstanding):
                return True


class AiAgentRunStep(models.Model):
    _name = "ai.agent.run.step"
    _description = "AI Agent Run Step"
    _order = "run_id, sequence"

    run_id = fields.Many2one("ai.agent.run", required=True, ondelete="restrict")
    sequence = fields.Integer(required=True)
    tool_id = fields.Many2one("ai.agent.tool", ondelete="restrict")
    call_id = fields.Many2one("ai.agent.call", ondelete="restrict")
    args = fields.Text()
    result = fields.Text()
    failed = fields.Boolean(default=False)
    dispatch_state = fields.Selection(
        [
            (s, s.title())
            for s in ("legacy", "observed", "refused", "failed", "dispatched")
        ],
        default="legacy",
        required=True,
    )
    _unique_step = models.Constraint(
        "UNIQUE(run_id, sequence)", "Step already recorded."
    )

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(self.env._("Audit rows are server-owned."))

    def write(self, vals):
        raise AccessError(self.env._("Audit rows are immutable."))

    def unlink(self):
        raise AccessError(self.env._("Audit history cannot be deleted."))

    @api.model
    def _new(self, vals):
        return super(AiAgentRunStep, self.sudo().with_context({})).create(vals)
