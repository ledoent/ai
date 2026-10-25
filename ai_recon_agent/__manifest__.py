{
    "name": "AI Reconciliation Agent",
    "summary": "Snapshot-bound invoice matching with reviewed, earned authority",
    "version": "20.0.1.0.0",
    "license": "AGPL-3",
    "author": "Odoo Community Association (OCA)",
    "maintainers": ["dnplkndll"],
    "website": "https://github.com/OCA/ai",
    "development_status": "Alpha",
    "category": "Accounting",
    "depends": ["ai_recon_review", "ai_agent_llm"],
    "data": [
        "security/ir.access.csv",
        "security/scope.xml",
        "data/tools.xml",
        "data/ir_cron.xml",
        "views/matching_views.xml",
    ],
}
