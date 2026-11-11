import logging
import re

from odoo import api, models
from odoo.exceptions import AccessError
from odoo.models import get_public_method

_logger = logging.getLogger(__name__)

# Methods whose effect a human would not expect an agent to be able to undo.
#
# Core carries no marker for this, so unlike everything else in this module the
# list below is a *heuristic we are asserting*, not a fact we are reading. It is
# deliberately kept here, in the open, so a reviewer can disagree with a
# specific entry rather than with an opaque number.
#
# Matched with re.match against a name that never starts with "_" (those are
# rejected upstream by get_public_method), so every pattern is written fully
# anchored "^...$". A pattern that cannot match such a name is dead weight that
# silently shrinks the irreversible count -- test_classifier.py asserts each one
# is reachable, because exactly that happened to an earlier `_(send_mail|...)$`
# entry here.
IRREVERSIBLE_PATTERNS = (
    r"^unlink$",
    # NB: no "archive" here. action_archive is the canonical *reversible*
    # soft-delete -- action_unarchive undoes it (odoo/orm/models.py:5279, :5290).
    r"^action_(post|cancel|done|confirm|validate)$",
    r"^button_(validate|confirm|cancel|draft|immediate_\w+)$",
    r"^action_(send|invoice)_\w+$",
    r"^action_\w+_send$",
    # mail.template.send_mail, and the post_* family.
    r"^(send|post)_\w+$",
    # message_post and friends mail the followers and cannot be recalled.
    r"^message_(post|mail|notify)\w*$",
)
_IRREVERSIBLE_RE = tuple(re.compile(p) for p in IRREVERSIBLE_PATTERNS)

READONLY = "readonly"
WRITING = "writing"
IRREVERSIBLE = "irreversible"


class AiAgentSurface(models.AbstractModel):
    """Report which methods an agent could reach as the calling user."""

    _name = "ai.agent.surface"
    _description = "AI Agent Callable Surface"

    # -- the three core predicates ------------------------------------------

    @api.model
    def _is_callable_remotely(self, record, name):
        """Return the method if ``/json/2`` would dispatch to it, else None.

        Four lines lifted from ``api_doc``'s ``is_public_method``
        (``addons/api_doc/controllers/api_doc.py:315-320``). Inlined rather than
        imported: that helper lives in a controller, and importing controller
        code into a model would drag ``api_doc`` and its asset bundles into the
        dependency closure to gain nothing. ``get_public_method`` itself is a
        public export of ``odoo.models``, which is how ``json2.py`` imports it.
        """
        try:
            method = get_public_method(record, name)
        except (AttributeError, AccessError):
            return None
        return None if hasattr(method, "__deprecated__") else method

    @api.model
    def _is_readonly(self, record, name):
        """Return True if ``/json/2`` would route this call to a readonly cursor.

        A **replica** of ``WebJson2Controller._web_json_2_rpc_readonly``
        (``addons/rpc/controllers/json2.py:22-34``), not a call to it: the
        original resolves its model through ``request.registry``, and ``request``
        is a ``LocalProxy`` that raises outside a request, so it is unreachable
        from a model or a non-HTTP test. Kept line-for-line so the two can be
        diffed, including the most-derived-first walk that lets an override win,
        and resolving through the registry for the same reason.
        """
        for cls in self.env.registry[record._name].mro():
            method = getattr(cls, name, None)
            if method is not None and hasattr(method, "_readonly"):
                return method._readonly
        return False

    @api.model
    def _classify(self, record, name):
        """Bucket a callable method. Read-only wins: a read-only cursor cannot
        write, so it cannot be irreversible however the name reads."""
        if self._is_readonly(record, name):
            return READONLY
        return IRREVERSIBLE if any(r.match(name) for r in _IRREVERSIBLE_RE) else WRITING

    # -- the scan ------------------------------------------------------------

    @api.model
    def _scan(self):
        """Return the surface reachable by ``self.env.user``.

        Deliberately underscore-private: a module whose purpose is auditing the
        remotely-callable surface must not add itself to it. ``tests/
        test_private_gate.py`` asserts that ``/json/2`` refuses this method.

        Call it as a specific user with ``with_user()``::

            env["ai.agent.surface"].with_user(u)._scan()
        """
        models_out = {}
        counts = {READONLY: 0, WRITING: 0, IRREVERSIBLE: 0}

        # pylint: disable=no-search-all
        # Scanning every model is the entire point: a partial sweep would
        # under-report the surface, which is the one way this module could be
        # actively misleading. ir.model holds a few hundred rows.
        for ir_model in self.env["ir.model"].sudo().search([]):
            model_name = ir_model.model
            # Stale ir.model rows outlive their class; env[...] would raise
            # KeyError, which over HTTP becomes a 500. Core carries the same
            # guard at api_doc.py:162 and a test for it at test_doc.py:276-290.
            if model_name not in self.env:
                continue

            record = self.env[model_name]
            if not record.has_access("read"):
                continue

            methods = {}
            for name in dir(record):
                # Enumeration only ever *inspects*. Never call an enumerated
                # method: logging.captureWarnings(True) is on (odoo/netsvc.py:49)
                # with DeprecationWarning at 'default', and Odoo's @deprecated
                # fires on call rather than on attribute access
                # (odoo/tools/func.py:287-295) -- so a probe-by-calling variant
                # of this loop would emit WARNINGs on py.warnings and fail
                # checklog-odoo.
                if not self._is_callable_remotely(record, name):
                    continue
                kind = self._classify(record, name)
                methods[name] = kind
                counts[kind] += 1

            if methods:
                models_out[model_name] = methods

        return {
            "user": self.env.user.login,
            "model_count": len(models_out),
            "method_count": sum(counts.values()),
            "by_class": counts,
            "models": models_out,
        }

    @api.model
    def _log_scan(self):
        """Log the headline figures at INFO. Safe under ``checklog-odoo``."""
        result = self._scan()
        _logger.info(
            "ai.agent.surface: user %s can call %d methods across %d models "
            "(%d readonly, %d writing, %d irreversible-by-heuristic)",
            result["user"],
            result["method_count"],
            result["model_count"],
            result["by_class"][READONLY],
            result["by_class"][WRITING],
            result["by_class"][IRREVERSIBLE],
        )
        return result
