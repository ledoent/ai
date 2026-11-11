# Capability ladder verification

The five-addon suite tests the runtime alongside the existing banking, clustering,
deduplication and review-setting regressions. Test method inventory:

| Addon            | Methods |
| ---------------- | ------: |
| ai_agent_surface |      26 |
| ai_agent_runtime |      71 |
| ai_agent_llm     |      33 |
| ai_agent_review  |      18 |
| ai_recon_review  |      44 |
| **Addon total**  | **192** |

Fresh installation also enters two core web harness tests. Those entries are not browser
QA results. Odoo's `tests.stats` includes setup and teardown; count actual
`Starting …test_…` entries when comparing methods.

## Full suites

Install all five addons into a fresh database with
`--skip-auto-install --test-enable --stop-after-init --log-level=info` and these test
tags:

```text
/ai_agent_surface,/ai_agent_runtime,/ai_agent_llm,/ai_agent_review,/ai_recon_review
```

The reference Python 3.12 rig uses packaged `odoo:20` (20.0-20261015). The second uses
the cached `odoo-py315:test` image, Python **3.15.0rc3**, mounting a 20.0 core checkout.
That release candidate is distinct from final Python 3.15.

Update `ai_agent_runtime` and `ai_agent_llm` together. The latter also adds frozen
provider and budget columns. A module update must include `-u`; starting an already
installed database without installation/update runs no addon tests.

## Upgrade from c150796

Use a disposable database and a Docker-shared directory containing
`git archive c150796`. Install the five baseline addons. Through `odoo shell`, feed
`tools/seed_capability_upgrade.py` on standard input. It creates active and archived
grants, a draft, running and pending runs, and completed audit history.

Switch the addon mount to the current source and update all five addons. Feed
`tools/verify_capability_upgrade.py` through the shell. It asserts that all three grants
start at observe, both unsafe runs are cancelled with a snapshot error, the draft and
completed audit survive, old steps are explicitly legacy, and no historical evidence
exists. It also exercises a new snapshot from the old draft. The shell rolls that last
probe back.

Runtime test fixtures reuse existing unique tool targets inside their rolled-back
transactions so the full suite can also run against the seeded upgrade database.

## Real PostgreSQL races

On a separate disposable installed database, feed
`tools/verify_capability_concurrency.py` through `odoo shell`. Do not use a production
database: it deliberately commits fixtures and registers a test model. Each worker uses
an independent connection and repeatable-read transaction.

Four paired races verify:

1. One dispatched invocation, one audit row and one business write.
2. One undo signal and one reversal, with immediate removal of apply authority.
3. One accepted judgment and one positive sample.
4. One promotion, with no reuse of the previous epoch's samples.

The harness retries serialization/uniqueness conflicts with a new transaction. A
competing dispatch or promotion can commit a no-op; assertions inspect the committed
values and counts. Expected serialization conflicts appear in its SQL log. Only
fixture-schema registry signaling is patched to retain the process-only model.
Production execution, authorization, locks and constraints are unchanged.

## Adapter proof

`tests/adapter_fixture.py` registers a second, database-local vertical only during
tests. Its adapter locks the subject, captures a JSON receipt and checks a monotonically
increasing revision before reversal, including ABA changes within the same transaction
timestamp. Undo restores the business value while advancing the revision, preserving
change detection.

Tests cover both promotions, one sample per call, failed execution and receipt capture
rollback, atomic reversal failure, unsafe reversal refusal, guarded irreversible
targets, manual overrides and reviewer notifications. The fixture is not a production
adapter. Banking proposals remain outside runtime evidence.

Run `pre-commit run --all-files` with new files staged. The fork's Odoo/OCB CI matrix
remains separate evidence from local runs. No deployment or upstream submission is part
of this verification.
