def migrate(cr, version):
    # Old pending args are not trustworthy invocation snapshots.
    cr.execute("""
        UPDATE ai_agent_run
           SET state = 'cancelled',
               error = 'Cancelled during capability upgrade: no trustworthy invocation snapshot.'
         WHERE state IN ('running', 'awaiting_approval')
    """)
