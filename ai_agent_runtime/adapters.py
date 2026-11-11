"""Trusted addon contract for database-local reversal, never administrator data.

Implementations must lock subjects in prepare(), capture a JSON receipt after the
call, and check for intervening changes under the same locks in reverse(). All
three operations run inside savepoints with the original execution identity.
They must never commit or dispatch external effects. Bump contract_id whenever
the implementation or receipt format changes. Bind on the target model through
its private _ai_reversal_adapter(method_name) hook.
"""

from contextlib import contextmanager
from contextvars import ContextVar

_invocation = ContextVar("ai_agent_invocation", default=None)


@contextmanager
def invocation_scope(call):
    """Private, one-transaction binding installed only by the dispatcher."""
    token = _invocation.set(
        (
            call.id,
            call.run_id.id,
            call.user_id.id,
            call.model_name,
            call.method_name,
            call.args,
            call.policy,
            call.capability_epoch,
            call.state,
            call.approver_id.id,
        )
    )
    try:
        yield
    finally:
        _invocation.reset(token)


def current_invocation():
    """Return the dispatcher binding to trusted addon code only."""
    return _invocation.get()


class ReversalAdapter:
    database_local = False
    target = None
    contract_id = None
    business_evidence_required = False

    def prepare(self, record, args):
        raise NotImplementedError

    def capture(self, record, prepared, result):
        raise NotImplementedError

    def reverse(self, record, receipt):
        raise NotImplementedError
