# pylint: disable=no-raise-unlink
from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class Profile(models.Model):
    _name = "ai.recon.profile"
    _description = "Company Matching Profile"
    _rec_name = "company_id"

    company_id = fields.Many2one("res.company", required=True, ondelete="restrict")
    journal_ids = fields.Many2many("account.journal", string="Frozen journal allowlist")
    agent_id = fields.Many2one("ai.agent", ondelete="restrict")
    enabled = fields.Boolean(default=False)
    llm_enabled = fields.Boolean(default=False)
    disclosure_opt_in = fields.Boolean(default=False)
    provider_id = fields.Many2one("ai.agent.provider", ondelete="restrict")
    revision = fields.Integer(default=1, readonly=True)
    intake_limit = fields.Integer(default=25, required=True)
    candidate_limit = fields.Integer(default=50, required=True)
    allocation_limit = fields.Integer(default=10, required=True)
    tolerance_ids = fields.One2many("ai.recon.tolerance", "profile_id")
    last_error = fields.Text(readonly=True)

    _company_unique = models.Constraint(
        "UNIQUE(company_id)", "Use one profile per company."
    )
    _agent_unique = models.Constraint(
        "UNIQUE(agent_id)", "Execution identities cannot span profiles."
    )

    @api.model_create_multi
    def create(self, vals_list):
        self.env["ai.agent"]._check_admin()
        if any(
            {"revision", "last_error", "tolerance_ids"}.intersection(v)
            or any(
                "default_" + f in self.env.context for f in ("revision", "last_error")
            )
            for v in vals_list
        ):
            raise AccessError(self.env._("Profile control fields are server-owned."))
        return super().create(vals_list)

    def write(self, vals):
        self.env["ai.agent"]._check_admin()
        if {"revision", "last_error", "tolerance_ids", "company_id"}.intersection(vals):
            raise AccessError(
                self.env._("Profile identity and control fields are protected.")
            )
        self._lock()
        result = super().write(vals)
        self._bump_policy()
        return result

    def unlink(self):
        self.env["ai.agent"]._check_admin()
        if self.filtered("agent_id"):
            raise UserError(
                self.env._("Disable provisioned profiles; retain their history.")
            )
        return super().unlink()

    def _lock(self):
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM ai_recon_profile WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(self.ids)],
        )
        self.invalidate_recordset()

    def _bump_policy(self):
        for profile in self:
            super(Profile, profile).write({"revision": profile.revision + 1})
            profile.agent_id.capability_ids._change(
                "Matching policy changed.", level="observe"
            )
        return True

    @api.constrains(
        "company_id",
        "journal_ids",
        "agent_id",
        "enabled",
        "llm_enabled",
        "disclosure_opt_in",
        "provider_id",
        "intake_limit",
        "candidate_limit",
        "allocation_limit",
    )
    def _check_profile(self):
        for p in self:
            if min(p.intake_limit, p.candidate_limit, p.allocation_limit) < 1:
                raise ValidationError(self.env._("Matching limits must be positive."))
            if p.journal_ids.filtered(
                lambda j: (
                    j.company_id != p.company_id
                    or j.type not in ("bank", "credit")
                    or not j.active
                )
            ):
                raise ValidationError(
                    self.env._("Choose active bank/credit journals in this company.")
                )
            if p.enabled and (not p.agent_id or not p.journal_ids):
                raise ValidationError(
                    self.env._(
                        "Provision an agent and select journals before enabling."
                    )
                )
            if p.agent_id and (
                p.agent_id.user_id.company_ids != p.company_id
                or p.agent_id.user_id.has_group("base.group_system")
                or p.agent_id.user_id.has_group("account.group_account_user")
                or not p.agent_id.irreversible_guard
            ):
                raise ValidationError(
                    self.env._(
                        "Use a single-company, non-administrative execution user without accounting sign-off rights."
                    )
                )
            if p.llm_enabled and (
                not p.disclosure_opt_in
                or not p.provider_id
                or p.provider_id.kind == "stub"
                or not p.provider_id.model
            ):
                raise ValidationError(
                    self.env._(
                        "LLM use requires disclosure opt-in and an explicit live provider/model."
                    )
                )

    def action_provision(self):
        self.env["ai.agent"]._check_admin()
        for p in self:
            if p.agent_id:
                continue
            reviewers = p.company_id.ai_recon_reviewer_ids.filtered(
                lambda u: u.active and u.has_group("account.group_account_user")
            )
            if not reviewers:
                raise UserError(
                    self.env._("Configure a qualified company reviewer first.")
                )
            identities = (
                self.env["ai.agent"]
                .with_context(active_test=False)
                .search([("user_id", "in", reviewers.ids)])
                .user_id
            )
            if reviewers & identities:
                raise UserError(self.env._("Execution accounts cannot be reviewers."))
            reviewers.group_ids += self.env.ref("ai_agent_runtime.group_ai_reviewer")
            user = (
                self.env["res.users"]
                .with_context(no_reset_password=True)
                .create(
                    {
                        "name": f"Matching executor: {p.company_id.name}",
                        "login": f"ai_matching_company_{p.company_id.id}",
                        "company_id": p.company_id.id,
                        "company_ids": [fields.Command.set(p.company_id.ids)],
                        "group_ids": [
                            fields.Command.set(
                                self.env.ref("account.group_account_invoice").ids
                            )
                        ],
                        "password": False,
                    }
                )
            )
            tools = self.env.ref("ai_recon_agent.tool_match_items") | self.env.ref(
                "ai_recon_agent.tool_post_match"
            )
            agent = self.env["ai.agent"].create(
                {
                    "name": f"Bank matching: {p.company_id.name}",
                    "user_id": user.id,
                    "tool_ids": [fields.Command.set(tools.ids)],
                    "reviewer_ids": [fields.Command.set(reviewers.ids)],
                }
            )
            p.agent_id = agent
        return True

    def _as_executor(self):
        self.ensure_one()
        return self.with_context(allowed_company_ids=self.company_id.ids).with_user(
            self.agent_id.user_id
        )

    def _check_ready(self):
        self.ensure_one()
        self._check_profile()
        if not self.enabled or not self.agent_id.active:
            raise UserError(self.env._("The matching profile is disabled."))
        self.env["ai.recon.proposal"]._assert_review_gate(self.company_id)
        expected = self.env.ref("ai_recon_agent.tool_match_items") | self.env.ref(
            "ai_recon_agent.tool_post_match"
        )
        if self.agent_id.tool_ids != expected:
            raise UserError(
                self.env._(
                    "Matching profiles require exactly their curated service grants."
                )
            )

    def action_intake(self):
        self.env["ai.agent"]._check_admin()
        for p in self:
            p._as_executor()._intake()
        return True


class Tolerance(models.Model):
    _name = "ai.recon.tolerance"
    _description = "Journal Difference Proposal Policy"

    profile_id = fields.Many2one("ai.recon.profile", required=True, ondelete="restrict")
    journal_id = fields.Many2one("account.journal", required=True, ondelete="restrict")
    amount_limit = fields.Float(default=0)
    percentage_limit = fields.Float(default=0)
    gain_account_id = fields.Many2one("account.account", ondelete="restrict")
    loss_account_id = fields.Many2one("account.account", ondelete="restrict")
    _journal_unique = models.Constraint(
        "UNIQUE(journal_id)", "One tolerance policy per journal."
    )

    @api.constrains(
        "profile_id",
        "journal_id",
        "amount_limit",
        "percentage_limit",
        "gain_account_id",
        "loss_account_id",
    )
    def _check_values(self):
        for p in self:
            accounts = p.gain_account_id | p.loss_account_id
            if (
                p.journal_id not in p.profile_id.journal_ids
                or p.amount_limit < 0
                or not 0 <= p.percentage_limit <= 100
                or accounts.filtered(
                    lambda a: (
                        p.profile_id.company_id not in a.company_ids
                        or not a.active
                        or a.account_type
                        not in (
                            "income",
                            "income_other",
                            "expense",
                            "expense_direct_cost",
                        )
                    )
                )
            ):
                raise ValidationError(
                    self.env._("Invalid journal tolerance or difference account.")
                )

    @api.model_create_multi
    def create(self, vals_list):
        self.env["ai.agent"]._check_admin()
        records = super().create(vals_list)
        records.profile_id._bump_policy()
        return records

    def write(self, vals):
        self.env["ai.agent"]._check_admin()
        if {"profile_id", "journal_id"}.intersection(vals):
            raise AccessError(
                self.env._("Recreate a tolerance policy to change its scope.")
            )
        result = super().write(vals)
        self.profile_id._bump_policy()
        return result

    def unlink(self):
        self.env["ai.agent"]._check_admin()
        profiles = self.profile_id
        result = super().unlink()
        profiles._bump_policy()
        return result
