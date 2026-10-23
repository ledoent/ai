{
    "name": "AI Agent Runtime",
    "summary": "Durable multi-step agent runs with an allowlist and an approval gate",
    "version": "20.0.1.0.0",
    "license": "AGPL-3",
    "author": "Odoo Community Association (OCA)",
    "maintainers": ["dnplkndll"],
    "website": "https://github.com/OCA/ai",
    "development_status": "Alpha",
    "category": "Productivity/AI",
    "depends": [
        "ai_agent_surface",
    ],
    "data": [
        "security/ir.access.csv",
        "views/ai_agent_views.xml",
        "views/ai_agent_run_views.xml",
        "data/ir_cron.xml",
    ],
    "demo": [],
}
