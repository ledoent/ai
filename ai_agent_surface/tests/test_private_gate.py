from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import SurfaceHttpCommon

# Every escape hatch an LLM would reach for first. All six are @api.private in
# odoo/orm/models.py (browse :5365, with_env :5428, sudo :5437, with_user :5464,
# with_context :5503, exists :5043), so get_public_method refuses them before
# dispatch. `exists` is the surprising one -- an agent cannot even test for the
# existence of a record it cannot read.
PRIVATE_ESCAPES = ["browse", "sudo", "with_user", "with_env", "with_context", "exists"]


@tagged("post_install", "-at_install")
class TestPrivateGate(SurfaceHttpCommon):
    """The privilege-escalation hatches are closed over ``/json/2``.

    Core proves bearer auth works and that *one* model-level denial returns 403
    (``test_webjson2.py:172-192``). It does not prove that the ORM's context
    switchers are unreachable, which is the property an agent design actually
    rests on: if ``sudo`` were callable, every other control here is decoration.
    """

    def test_private_escapes_are_403(self):
        for method in PRIVATE_ESCAPES:
            with self.subTest(method=method):
                # An AccessError that reaches the HTTP layer logs at WARNING on
                # 'odoo.http' (odoo/http/router.py:306-307), and
                # OCA_ENABLE_CHECKLOG_ODOO makes any WARNING a CI failure --
                # so the wrapper is load-bearing, not hygiene. mute_logger on
                # the child keeps the INFO access line out of the capture, as
                # core does at api_doc/tests/test_doc.py:37-50.
                with (
                    mute_logger("odoo.http.server"),
                    self.assertLogs("odoo.http") as capture,
                ):
                    res = self._json2("res.partner", method)
                self.assertEqual(res.status_code, 403, res.text)
                expected = (
                    f"Private methods (such as 'res.partner.{method}') "
                    "cannot be called remotely."
                )
                self.assertEqual(res.json()["message"], expected)
                self.assertEqual(capture.output, [f"WARNING:odoo.http:{expected}"])

    def test_public_method_is_reachable(self):
        """Positive control: the same helper gets a 200 on a public method.

        Without this, every 403 above could be an artifact of a malformed
        request rather than evidence of the gate -- and that is not a
        hypothetical. An earlier draft of ``_json2`` sent GET instead of POST,
        landed on the ``/json/2/<path:subpath>`` catch-all, and got a uniform
        404 that was indistinguishable from a refusal. A suite of negative
        assertions with no positive control cannot tell the difference between
        "the gate held" and "my request was broken".
        """
        res = self._json2("res.partner", "search_count", domain=[])
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIsInstance(res.json(), int)

    def test_scan_method_is_not_reachable(self):
        """This module does not add itself to the surface it measures."""
        with mute_logger("odoo.http.server"), self.assertLogs("odoo.http"):
            res = self._json2("ai.agent.surface", "_scan")
        self.assertEqual(res.status_code, 403, res.text)

    def test_missing_method_is_404_and_silent(self):
        """A nonexistent method is 404 and logs nothing -- unlike a 403.

        The asymmetry is the interesting part. ``get_public_method`` raises
        ``AttributeError`` for an absent method, which ``json2.py:71-74`` turns
        into a ``NotFound``; ``router.py:302-303`` passes on ``HTTPException``
        without logging. So "refused because private" is observable in the log
        while "does not exist" is not -- which matters to anyone planning to
        detect agent probing from the server log.
        """
        with mute_logger("odoo.http.server"):
            res = self._json2("res.partner", "no_such_method_at_all")
        self.assertEqual(res.status_code, 404)
