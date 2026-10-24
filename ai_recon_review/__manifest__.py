{
    "name": "AI Reconciliation Review",
    "summary": "Stage proposed bank reconciliations for human review in a daily batch",
    "version": "20.0.1.0.0",
    "license": "AGPL-3",
    "author": "Odoo Community Association (OCA)",
    "maintainers": ["dnplkndll"],
    "website": "https://github.com/OCA/ai",
    "development_status": "Alpha",
    "category": "Accounting",
    "depends": [
        "ai_agent_runtime",
        "account",
    ],
    "data": [
        "security/ir.access.csv",
        "data/mail_template.xml",
        "data/ir_cron.xml",
        "views/ai_recon_views.xml",
    ],
    "demo": [],
}
