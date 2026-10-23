import inspect
import json
import logging

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

STATE_SELECTION = [
    ("draft", "Draft"),
    ("running", "Running"),
    ("awaiting_approval", "Awaiting approval"),
    ("done", "Done"),
    ("failed", "Failed"),
    ("cancelled", "Cancelled"),
]


class AiAgentRun(models.Model):
    """One durable, resumable execution of a plan.

    A separate record rather than state held in a cron, because ``ir.cron``
    offers nowhere to put it: ``_trigger`` takes no payload
    (``odoo/addons/base/models/ir_cron.py``), so anything a job needs to
    remember between steps has to live in a model. That is a requirement core
    imposes, not a design preference.
    """

    _name = "ai.agent.run"
    _description = "AI Agent Run"
    _order = "id desc"

    name = fields.Char(compute="_compute_name")
    agent_id = fields.Many2one("ai.agent", required=True, ondelete="restrict")
    state = fields.Selection(STATE_SELECTION, default="draft", required=True)
    plan = fields.Text(
        required=True,
        default="[]",
        help='JSON list of steps, e.g. [{"tool": "count_partners", "args": {}}]. '
        "Written by hand for now; a planner would produce it later.",
    )
    step_no = fields.Integer(
        default=0,
        readonly=True,
        help="Index of the next plan entry to execute. The resume cursor.",
    )
    step_ids = fields.One2many("ai.agent.run.step", "run_id", readonly=True)
    pending_tool_id = fields.Many2one("ai.agent.tool", readonly=True)
    pending_args = fields.Text(readonly=True)
    approver_id = fields.Many2one("res.users", readonly=True)
    error = fields.Text(readonly=True)

    @api.depends("agent_id", "state")
    def _compute_name(self):
        for run in self:
            run.name = f"{run.agent_id.name or '?'} #{run.id or 0}"

    # -- plan access ---------------------------------------------------------

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

    # -- the executor --------------------------------------------------------

    def _resolve_tool(self, name):
        """Resolve a plan step's tool name against the agent's allowlist.

        Deliberately resolves through ``agent_id.tool_ids`` and not through a
        global search: a tool record existing is not authorisation to use it.
        This is the allowlist that ``ai_agent_surface`` showed Odoo's own access
        rights cannot express.
        """
        self.ensure_one()
        tool = self.agent_id._tool_by_name(name)
        if not tool:
            raise AccessError(
                self.env._(
                    "Agent %(agent)s is not allowed to use a tool named %(tool)s.",
                    agent=self.agent_id.name,
                    tool=name,
                )
            )
        return tool

    def _execute_tool(self, tool, args):
        """Call the tool as the agent's user. Never as sudo.

        ``with_user`` and not ``sudo``, because ``sudo()`` has no audit mode:
        every access predicate short-circuits under ``env.su``
        (``odoo/orm/models.py:3469``), so a sudo call cannot report what the
        agent's own identity would have been refused. Running as the user means
        an unauthorised call simply raises, which is the behaviour we want.
        """
        self.ensure_one()
        record = self.env[tool.model_name].with_user(self.agent_id.user_id)
        if not self.env["ai.agent.surface"]._is_callable_remotely(
            record, tool.method_name
        ):
            # Defence in depth: ai.agent.tool's constraint already rejects
            # these at definition time, but a method can stop being public
            # between then and now (a module uninstall, a patch).
            raise AccessError(
                self.env._(
                    "%(model)s.%(method)s is not remotely callable.",
                    model=tool.model_name,
                    method=tool.method_name,
                )
            )

        bound = getattr(record, tool.method_name)
        # Validate the arguments against the real signature before calling,
        # the way /json/2 does (addons/rpc/controllers/json2.py:80-84, which
        # turns a bind failure into a 422). Wrong or missing arguments are the
        # single most likely defect in a plan -- especially once a planner
        # rather than a human writes it -- and without this they surface as a
        # raw TypeError that escapes the run instead of failing it cleanly with
        # a message anyone can act on.
        try:
            inspect.signature(bound).bind(**args)
        except TypeError as exc:
            raise UserError(
                self.env._(
                    "Tool %(tool)s was called with arguments that do not match "
                    "%(model)s.%(method)s: %(err)s",
                    tool=tool.name,
                    model=tool.model_name,
                    method=tool.method_name,
                    err=exc.args[0] if exc.args else exc,
                )
            ) from exc

        return bound(**args)

    def _advance(self):
        """Execute at most one plan step. Returns True if there is more to do.

        One step per call, with the cursor and the step record written in the
        same transaction, so a crash can never leave a step executed but
        unrecorded -- or recorded twice. The unique constraint on
        (run_id, sequence) is what makes the second case impossible rather than
        merely unlikely.
        """
        self.ensure_one()
        if self.state != "running":
            return False

        entries = self._plan_entries()
        if self.step_no >= len(entries):
            self.state = "done"
            return False

        entry = entries[self.step_no]
        tool = self._resolve_tool(entry.get("tool"))
        args = entry.get("args") or {}

        if tool.requires_approval and not self.approver_id:
            self.write(
                {
                    "state": "awaiting_approval",
                    "pending_tool_id": tool.id,
                    "pending_args": json.dumps(args),
                }
            )
            return False

        try:
            result = self._execute_tool(tool, args)
        except (AccessError, UserError) as exc:
            self.write({"state": "failed", "error": str(exc)})
            self.env["ai.agent.run.step"].create(
                {
                    "run_id": self.id,
                    "sequence": self.step_no,
                    "tool_id": tool.id,
                    "args": json.dumps(args),
                    "result": str(exc),
                    "failed": True,
                }
            )
            return False

        self.env["ai.agent.run.step"].create(
            {
                "run_id": self.id,
                "sequence": self.step_no,
                "tool_id": tool.id,
                "args": json.dumps(args),
                "result": repr(result)[:2000],
            }
        )
        self.write(
            {
                "step_no": self.step_no + 1,
                # An approval authorises one step, not the rest of the run.
                "approver_id": False,
                "pending_tool_id": False,
                "pending_args": False,
            }
        )
        if self.step_no >= len(entries):
            self.state = "done"
            return False
        return True

    # -- user-facing transitions --------------------------------------------

    def action_start(self):
        for run in self:
            if run.state not in ("draft", "awaiting_approval"):
                raise UserError(
                    self.env._("Run %(name)s is not startable.", name=run.name)
                )
            run.state = "running"
        return True

    def action_approve(self):
        """Approve exactly the pending step, then continue."""
        for run in self:
            if run.state != "awaiting_approval":
                raise UserError(
                    self.env._("Run %(name)s is not awaiting approval.", name=run.name)
                )
            run.write({"approver_id": self.env.uid, "state": "running"})
        return True

    def action_reject(self):
        for run in self:
            if run.state != "awaiting_approval":
                raise UserError(
                    self.env._("Run %(name)s is not awaiting approval.", name=run.name)
                )
            run.write({"state": "cancelled", "error": "Rejected by approver."})
        return True

    def action_run_to_completion(self):
        """Drain this run synchronously. Convenience for tests and the UI."""
        for run in self:
            while run._advance():
                pass
        return True

    # -- the drain -----------------------------------------------------------

    @api.model
    def _cron_drain(self):
        """Advance every running run, one step at a time, committing as we go.

        Uses ``_commit_progress`` so a long queue is drained across several cron
        acquisitions instead of monopolising one worker: core's own
        ``sale.order._cron_send_pending_emails`` is the reference shape. Note
        this does **not** make runs concurrent -- one cron definition is still
        one execution at a time, which is a property of ``_acquire_one_job``
        locking a single ``ir_cron`` row. Quantifying that is E1's job.
        """
        runs = self.search([("state", "=", "running")])
        Cron = self.env["ir.cron"]
        Cron._commit_progress(remaining=len(runs))
        for run in runs:
            run = run.with_prefetch()
            try:
                run.action_run_to_completion()
            except Exception:
                _logger.exception("ai.agent.run %s drain failed", run.id)
                run.write({"state": "failed", "error": "Unhandled error; see log."})
            if not Cron._commit_progress(processed=1):
                break
        return True


class AiAgentRunStep(models.Model):
    """The audit trail: one row per executed step, append-only in practice."""

    _name = "ai.agent.run.step"
    _description = "AI Agent Run Step"
    _order = "run_id, sequence"

    run_id = fields.Many2one("ai.agent.run", required=True, ondelete="cascade")
    sequence = fields.Integer(required=True)
    tool_id = fields.Many2one("ai.agent.tool", required=True, ondelete="restrict")
    args = fields.Text()
    result = fields.Text()
    failed = fields.Boolean(default=False)

    _unique_step = models.Constraint(
        "UNIQUE(run_id, sequence)",
        "A step with that sequence has already been recorded for this run.",
    )
