"""
Single source of truth for page resources and their valid actions.

Both the permission matrix UI and route enforcement read from RESOURCES.
Actions not listed for a resource cannot be assigned — no DB rows, no checkboxes.
"""

RESOURCES = [
    {
        "module": "Main",
        "items": [
            {"name": "dashboard", "label": "Dashboard", "actions": ["read"]},
        ],
    },
    {
        "module": "Management",
        "items": [
            {"name": "companies", "label": "Companies", "actions": ["read", "create", "write", "delete"]},
            {"name": "users",     "label": "Users",     "actions": ["read", "create", "write", "delete"]},
        ],
    },
    {
        "module": "Messaging",
        "items": [
            {"name": "services",       "label": "Services",       "actions": ["read"]},
            {"name": "conversations",  "label": "Conversations",  "actions": ["read"]},
            {"name": "erpnext_configs","label": "ERPNext Config",  "actions": ["read", "create", "write", "delete"]},
        ],
    },
    {
        "module": "Analytics",
        "items": [
            {"name": "reports", "label": "Reports & Analytics", "actions": ["read"]},
        ],
    },
]

# Flat lookup: page_name -> set of valid actions
VALID_ACTIONS: dict[str, set[str]] = {
    item["name"]: set(item["actions"])
    for group in RESOURCES
    for item in group["items"]
}

ALL_PAGE_NAMES: list[str] = list(VALID_ACTIONS.keys())

# Default permission rows for seed data
ADMIN_DEFAULT_ACTIONS = {"read", "create", "write", "delete"}
COMPANY_VIEWER_PAGES  = {"dashboard", "reports"}
