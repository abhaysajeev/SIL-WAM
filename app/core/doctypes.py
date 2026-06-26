"""
Doctype configs — single source of truth for list columns and form fields.

Each doctype drives:
  - list_view.html  →  what columns to render, how to render cells
  - form_view.html  →  what sections/fields to render, field types

Field types:
  text          plain text input
  code          mono-spaced text input (company codes etc.)
  phone         tel input
  password      password input
  checkbox      boolean toggle (styled)
  textarea      multi-line text
  select_role   <select> populated from `roles` context
  select_company <select> populated from `companies` context

Cell types (list view only):
  text          plain value
  mono          mono-spaced (username, codes)
  status        green Active / red Inactive badge
  role_tag      resolved role display_name badge
  company_tag   resolved company name

Field options:
  required      bool
  create_only   bool  — renders on new form only; shows static on edit
  hint          str   — small help text below input
  placeholder   str
  maxlength     int
  minlength     int
  cols          1|2   — column span in 2-col grid (default 1)
"""

ERROR_LOGS_DOCTYPE = {
    "resource":     "error_logs",
    "title":        "Error Log",
    "title_plural": "Error Logs",
    "perm_page":    "dashboard",   # not used — access controlled by role_name check
    "icon":         "fas fa-bug",
    "api_prefix":   "/api/error-logs",
    "base_route":   "/error-logs",
    "title_field":  "title",

    "list_columns": [
        {"label": "Title",  "field": "title",      "type": "text", "bold": True},
        {"label": "Method", "field": "method",     "type": "mono"},
        {"label": "Type",   "field": "error_type", "type": "mono"},
        {"label": "User",   "field": "user",       "type": "text"},
        {"label": "Seen",   "field": "seen",       "type": "status"},
        {"label": "Time",   "field": "created_at", "type": "text"},
    ],

    "form_sections": [],  # no create/edit — detail page is custom
}

COMPANIES_DOCTYPE = {
    "resource":       "companies",
    "title":          "Company",
    "title_plural":   "Companies",
    "perm_page":      "companies",
    "icon":           "fas fa-building",
    "api_prefix":     "/api/companies",
    "base_route":     "/companies",
    "title_field":    "name",

    "list_columns": [
        {"label": "Company Name", "field": "name",         "type": "text",   "bold": True},
        {"label": "Code",         "field": "company_code", "type": "mono"},
        {"label": "Status",       "field": "is_active",    "type": "status"},
    ],

    "form_sections": [
        {
            "label": "Company Information",
            "fields": [
                {
                    "name": "name", "label": "Company Name",
                    "type": "text", "required": True, "maxlength": 200,
                    "placeholder": "e.g. Acme Corporation", "cols": 2,
                },
                {
                    "name": "company_code", "label": "Company Code",
                    "type": "code", "required": True, "maxlength": 50,
                    "placeholder": "e.g. ACME",
                    "hint": "Uppercase letters, numbers, hyphens, underscores.",
                },
                {
                    "name": "is_active", "label": "Active",
                    "type": "checkbox",
                },
            ],
        },
    ],
}


ERPNEXT_CONFIG_DOCTYPE = {
    "resource":     "erpnext_configs",
    "title":        "ERPNext Config",
    "title_plural": "ERPNext Configs",
    "perm_page":    "erpnext_configs",
    "icon":         "ti ti-plug-connected",
    "api_prefix":   "/api/erpnext-configs",
    "base_route":   "/erpnext-configs",
    "title_field":  "base_url",

    "list_columns": [
        {"label": "ERPNext URL", "field": "base_url",     "type": "mono",   "bold": True},
        {"label": "Company",     "field": "company_name", "type": "text"},
        {"label": "PDF Method",  "field": "pdf_method",   "type": "mono"},
        {"label": "Status",      "field": "is_active",    "type": "status"},
        {"label": "Created",     "field": "created_at",   "type": "text"},
    ],

    "form_sections": [
        {
            "label": "Connection",
            "fields": [
                {
                    "name": "company_id", "label": "Company",
                    "type": "select_company", "required": True,
                    "create_only": True,
                    "hint": "One ERPNext config per company. Cannot be changed after creation.",
                },
                {
                    "name": "base_url", "label": "ERPNext URL",
                    "type": "text", "required": True, "cols": 2,
                    "placeholder": "https://erp.yourcompany.com",
                    "hint": "No trailing slash.",
                },
                {
                    "name": "api_key", "label": "API Key",
                    "type": "text", "required": True,
                    "placeholder": "ERPNext API key",
                },
                {
                    "name": "api_secret", "label": "API Secret",
                    "type": "secret", "required": True,
                    "placeholder": "Enter new API secret",
                },
                {
                    "name": "pdf_method", "label": "PDF Method",
                    "type": "text", "cols": 2,
                    "placeholder": "custom_app.api.send_invoice_pdf",
                    "hint": "Leave blank to use the global default (ERPNEXT_PDF_METHOD).",
                },
                {
                    "name": "is_active", "label": "Active",
                    "type": "checkbox",
                },
            ],
        },
    ],
}

SERVICES_DOCTYPE = {
    "resource":     "services",
    "title":        "Service",
    "title_plural": "Services",
    "perm_page":    "services",
    "icon":         "ti ti-message-bolt",
    "base_route":   "/services",
    "title_field":  "service_id",

    "list_columns": [
        {"label": "Service ID",  "field": "service_id",   "type": "mono",           "bold": True},
        {"label": "Company",     "field": "company_name", "type": "text"},
        {"label": "Status",      "field": "status",       "type": "service_status"},
        {"label": "Progress",    "field": "progress",     "type": "text"},
        {"label": "Created",     "field": "created_at",   "type": "text"},
        {"label": "Completed",   "field": "completed_at", "type": "text"},
    ],

    "form_sections": [],  # read-only — created via client API only
}

CONVERSATIONS_DOCTYPE = {
    "resource":     "conversations",
    "title":        "Conversation",
    "title_plural": "Conversations",
    "perm_page":    "conversations",
    "icon":         "ti ti-messages",
    "base_route":   "/conversations",
    "title_field":  "mobile_no",

    "list_columns": [
        {"label": "Mobile No",      "field": "mobile_no",       "type": "mono",           "bold": True},
        {"label": "Company",        "field": "company_name",    "type": "text"},
        {"label": "Messages",       "field": "total_messages",  "type": "text"},
        {"label": "Last Activity",  "field": "last_activity_at","type": "text"},
        {"label": "Active Service", "field": "active_service",  "type": "service_status"},
    ],

    "form_sections": [],  # read-only
}

USERS_DOCTYPE = {
    "resource":       "users",
    "title":          "User",
    "title_plural":   "Users",
    "perm_page":      "users",
    "icon":           "fas fa-user-shield",
    "api_prefix":     "/api/users",
    "base_route":     "/users",
    "title_field":    "username",

    "list_columns": [
        {"label": "Username",   "field": "username",      "type": "mono",        "bold": True},
        {"label": "Full Name",  "field": "full_name",     "type": "text"},
        {"label": "Phone",      "field": "phone",         "type": "text"},
        {"label": "Role",       "field": "_role_name",    "type": "role_tag"},
        {"label": "Company",    "field": "_company_name", "type": "text"},
        {"label": "Status",     "field": "is_active",     "type": "status"},
    ],

    "form_sections": [
        {
            "label": "Login Details",
            "fields": [
                {
                    "name": "username", "label": "Username",
                    "type": "text", "required": True,
                    "maxlength": 100, "create_only": True,
                    "placeholder": "e.g. john_doe",
                    "hint": "Cannot be changed after creation.",
                },
                {
                    "name": "full_name", "label": "Full Name",
                    "type": "text", "required": True, "maxlength": 200,
                    "placeholder": "John Doe",
                },
                {
                    "name": "phone", "label": "Phone",
                    "type": "phone", "maxlength": 20,
                    "placeholder": "+91 9000000000",
                },
            ],
        },
        {
            "label": "Access Control",
            "fields": [
                {
                    "name": "role_id", "label": "Role",
                    "type": "select_role",
                },
                {
                    "name": "company_id", "label": "Company",
                    "type": "select_company",
                    "hint": "Leave blank for all-company access (admin tier).",
                },
                {
                    "name": "is_active", "label": "Active",
                    "type": "checkbox",
                },
                {
                    "name": "must_change_password",
                    "label": "Must change password on next login",
                    "type": "checkbox",
                },
            ],
        },
        {
            "label": "Set Password",
            "create_only": True,
            "fields": [
                {
                    "name": "password", "label": "Password",
                    "type": "password", "required": True,
                    "minlength": 8, "placeholder": "Min 8 characters",
                },
            ],
        },
    ],
}
