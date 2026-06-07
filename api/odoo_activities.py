import os
from datetime import date, timedelta

ODOO_DB      = os.environ.get("ODOO_DB", "")
ODOO_API_KEY = os.environ.get("ODOO_API_KEY", "")

_MODEL_ID_CACHE = {}
_ACTIVITY_TYPE_CACHE = {}


def get_crm_lead_model_id(uid, models):
    if "crm.lead" in _MODEL_ID_CACHE:
        return _MODEL_ID_CACHE["crm.lead"]
    result = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "ir.model", "search_read",
        [[["model", "=", "crm.lead"]]],
        {"fields": ["id"], "limit": 1},
    )
    _MODEL_ID_CACHE["crm.lead"] = result[0]["id"]
    return _MODEL_ID_CACHE["crm.lead"]


def get_activity_type_id(uid, models, type_name):
    if type_name in _ACTIVITY_TYPE_CACHE:
        return _ACTIVITY_TYPE_CACHE[type_name]
    result = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "mail.activity.type", "search_read",
        [[["name", "ilike", type_name]]],
        {"fields": ["id", "name"], "limit": 1},
    )
    if not result:
        raise ValueError(f"Activity type '{type_name}' not found in Odoo")
    _ACTIVITY_TYPE_CACHE[type_name] = result[0]["id"]
    return _ACTIVITY_TYPE_CACHE[type_name]


def schedule_activity(uid, models, lead_id, activity_type="Phone Call", days_ahead=1):
    model_id = get_crm_lead_model_id(uid, models)
    type_id  = get_activity_type_id(uid, models, activity_type)
    due = (date.today() + timedelta(days=days_ahead)).isoformat()
    return models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "mail.activity", "create",
        [{
            "res_model_id":      model_id,
            "res_id":            lead_id,
            "activity_type_id":  type_id,
            "date_deadline":     due,
            "summary":           "Follow up with Facebook lead",
        }],
    )


def get_leads_without_activity(uid, models, user_id=None):
    lead_domain = [["active", "=", True]]
    if user_id:
        lead_domain.append(["user_id", "=", user_id])
    all_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "search", [lead_domain], {"limit": 5000},
    )
    if not all_ids:
        return []
    model_id = get_crm_lead_model_id(uid, models)
    acts = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "mail.activity", "search_read",
        [[["res_model_id", "=", model_id], ["res_id", "in", all_ids]]],
        {"fields": ["res_id"], "limit": 5000},
    )
    with_activity = {a["res_id"] for a in acts}
    return [i for i in all_ids if i not in with_activity]
