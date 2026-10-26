{
    "name": "AI Agent Usage",
    "summary": "Durable provider usage ledger and optional budget controls",
    "version": "20.0.1.0.0",
    "license": "AGPL-3",
    "author": "Odoo Community Association (OCA)",
    "maintainers": ["dnplkndll"],
    "website": "https://github.com/OCA/ai",
    "development_status": "Alpha",
    "category": "Productivity/AI",
    "depends": ["ai_agent_runtime", "ai_agent_llm"],
    "data": [
        "security/ir.access.csv",
        "security/ir_rule.xml",
        "views/ai_agent_usage_views.xml",
    ],
    "installable": True,
    "auto_install": False,
}
