{
    "name": "AI Reconciliation Agent",
    "summary": "Attributed, reviewed bank-to-document matching",
    "version": "20.0.1.0.0",
    "license": "AGPL-3",
    "author": "Odoo Community Association (OCA)",
    "maintainers": ["dnplkndll"],
    "website": "https://github.com/OCA/ai",
    "development_status": "Alpha",
    "category": "Accounting/Accounting",
    "depends": ["ai_recon_review", "ai_agent_llm"],
    "data": [
        "security/ir.access.csv",
        "security/ir_rule.xml",
        "views/ai_recon_agent_views.xml",
    ],
    "installable": True,
}
