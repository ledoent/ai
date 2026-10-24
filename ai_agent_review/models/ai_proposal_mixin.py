import types

from odoo import api, fields, models
from odoo.exceptions import UserError

CONFIDENCE = [
    ("high", "Confident"),
    ("medium", "Needs a human"),
    ("none", "No idea"),
]

STATE = [
    ("observed", "Observed"),
    ("proposed", "Proposed"),
    ("approved", "Approved"),
    ("applied", "Applied"),
    ("rejected", "Rejected"),
    ("noted", "Noted only"),
]


class AiProposalMixin(models.AbstractModel):
    """An agent-proposed action, held as data until someone agrees to it.

    The one idea worth keeping across every vertical: the proposal lives here,
    not as a half-written record. Nothing has happened, so there is nothing to
    block -- *"unapproved cannot take effect"* is true by construction rather
    than by freezing fields on a live record and maintaining a list of the ones
    that must stay writable. That list is where approval frameworks become
    brittle.

    A vertical inherits this and supplies: its own ``Many2one`` to the subject
    record, the payload fields, and the hooks at the bottom. The target stays a
    real foreign key on the concrete model rather than a generic
    ``res_model``/``res_id`` pair, so ``ondelete`` and uniqueness still work.
    """

    _name = "ai.proposal.mixin"
    _description = "AI Proposal Mixin"
    _order = "confidence, id"

    confidence = fields.Selection(CONFIDENCE, required=True, index=True)
    state = fields.Selection(STATE, default="proposed", required=True, index=True)
    rationale = fields.Text(help="Why the agent proposed this, in its own words.")
    approved_by = fields.Many2one("res.users", readonly=True)
    approved_date = fields.Datetime(readonly=True)

    # What each confidence tier is allowed to do. Overridable per vertical, and
    # the default is deliberately timid: `high` flags rather than applies.
    #
    # Auto-applying a confident match is defensible *only* where the action is
    # reversible. It is for bank reconciliation -- you can un-reconcile and
    # redo, so a wrong match costs cleanup. It is not for a sent dunning email,
    # a confirmed purchase order, or a price on a live pricelist. Shipping
    # `high -> apply` as the default would hand every vertical a safety
    # argument that holds for exactly one of them, so a vertical that wants it
    # must opt in and say why at the opt-in site.
    _confidence_actions = types.MappingProxyType(
        {"high": "flag", "medium": "flag", "none": "note"}
    )

    # -- the gate ------------------------------------------------------------

    def _execute(self):
        self.ensure_one()
        if self.state != "approved":
            raise UserError(
                self.env._(
                    "Proposal %(id)s is %(state)s, not approved. Nothing was done.",
                    id=self.id,
                    state=self.state,
                )
            )
        # Staging can precede approval by days; a configuration assumption
        # checked during routing must still hold at the point of dispatch.
        self._assert_preconditions()
        self._apply()
        self.state = "applied"
        return True

    def action_approve(self):
        # Validate the whole selection before recording a judgement or applying
        # its first item. A later invalid item must not leave earlier effects.
        if any(proposal.state not in ("proposed", "observed") for proposal in self):
            raise UserError(self.env._("Only a proposed line can be approved."))
        self._assert_preconditions()
        for proposal in self:
            proposal.write(
                {
                    "state": "approved",
                    "approved_by": self.env.uid,
                    "approved_date": fields.Datetime.now(),
                }
            )
            proposal._execute()
        return True

    def action_reject(self):
        for proposal in self:
            if proposal.state not in ("proposed", "observed"):
                raise UserError(self.env._("Only a proposed line can be rejected."))
            proposal.write(
                {
                    "state": "rejected",
                    "approved_by": self.env.uid,
                    "approved_date": fields.Datetime.now(),
                }
            )
        return True

    def action_undo(self):
        for proposal in self.filtered(lambda p: p.state == "applied"):
            proposal._undo()
            proposal.state = "proposed"
        return True

    # -- routing -------------------------------------------------------------

    @api.model
    def _route(self, proposals):
        """Send each proposal down the path its confidence earns."""
        proposals._assert_preconditions()
        for proposal in proposals:
            action = proposal._confidence_actions.get(proposal.confidence, "note")
            if action == "apply":
                proposal._apply()
                proposal.state = "applied"
            elif action == "flag":
                proposal._flag_for_attention()
            else:
                proposal.state = "noted"
        return True

    def _assert_preconditions(self):
        """Refuse to run if the vertical's safety assumptions do not hold.

        Exists because one of ours silently did not: the separation of duties
        for bank reconciliation depends on a company setting this code does not
        own, and with that setting off the whole guarantee evaporates. A
        vertical that rests on an external precondition should check it here
        and fail closed rather than document it and hope.
        """
        return True

    # -- hooks a vertical must supply ---------------------------------------

    def _apply(self):
        raise NotImplementedError

    def _flag_for_attention(self):
        """Mark the subject record so a human finds it. Default: do nothing.

        Most verticals have somewhere native to put this -- ``review_state``
        for accounting, ``activity_exception_decoration`` elsewhere. A vertical
        with nowhere to flag leaves the proposal visible only in its own list,
        which is honest but weaker.
        """
        self.ensure_one()
        return False

    def _undo(self):
        """Reverse an applied proposal. Optional.

        A vertical with no undo must not opt into ``high -> apply``; that is the
        whole basis on which auto-applying is defensible.
        """
        raise UserError(
            self.env._("This kind of proposal cannot be undone automatically.")
        )

    def _cluster_key(self):
        """A hashable grouping the 'no idea' pile into candidate rules."""
        self.ensure_one()

    @api.model
    def _cluster_label(self, key):
        """Render a cluster key for the email. Keeps the key shape private."""
        return str(key)

    @api.model
    def _draft_rule(self, key, proposals):
        """Create the deterministic artefact that would make this cluster
        unnecessary -- inactive, so a human still accepts it."""
        return
