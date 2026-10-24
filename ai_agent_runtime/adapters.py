"""Trusted addon contract for database-local reversal, never administrator data.

Implementations must lock subjects in prepare(), capture a JSON receipt after the
call, and check for intervening changes under the same locks in reverse(). All
three operations run inside savepoints with the original execution identity.
They must never commit or dispatch external effects. Bump contract_id whenever
the implementation or receipt format changes. Bind on the target model through
its private _ai_reversal_adapter(method_name) hook.
"""


class ReversalAdapter:
    database_local = False
    target = None
    contract_id = None

    def prepare(self, record, args):
        raise NotImplementedError

    def capture(self, record, prepared, result):
        raise NotImplementedError

    def reverse(self, record, receipt):
        raise NotImplementedError
