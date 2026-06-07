import os
import json
import sys
import xmlrpc.client
from datetime import date
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(__file__))
from odoo_activities import (
    ODOO_DB, ODOO_API_KEY,
    get_crm_lead_model_id,
    get_leads_without_activity,
)

ODOO_URL      = os.environ.get("ODOO_URL", "").strip()
ODOO_USERNAME = os.environ.get("ODOO_USERNAME", "").strip()
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "").strip()


def _odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_API_KEY, {})
    if not uid:
        raise ValueError("Odoo authentication failed")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def _build_dashboard(uid, models, user_id=None):
    today_str   = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()
    model_id    = get_crm_lead_model_id(uid, models)

    def xcall(model, method, domain, fields=None, limit=5000, context=None):
        kwargs = {"limit": limit}
        if fields:
            kwargs["fields"] = fields
        if context:
            kwargs["context"] = context
        return models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, model, method, [domain], kwargs)

    def act_count(extra):
        domain = [["res_model_id", "=", model_id]] + extra
        if user_id:
            domain.append(["user_id", "=", user_id])
        return len(xcall("mail.activity", "search", domain))

    def act_records(extra, fields):
        domain = [["res_model_id", "=", model_id]] + extra
        if user_id:
            domain.append(["user_id", "=", user_id])
        return xcall("mail.activity", "search_read", domain, fields)

    lead_base = [["active", "=", True]]
    if user_id:
        lead_base.append(["user_id", "=", user_id])

    overdue   = act_count([["date_deadline", "<", today_str]])
    due_today = act_count([["date_deadline", "=", today_str]])
    upcoming  = act_count([["date_deadline", ">", today_str]])

    total_active  = len(xcall("crm.lead", "search", lead_base))
    no_act_ids    = get_leads_without_activity(uid, models, user_id)

    won_domain = [
        ["active", "=", False],
        ["type",   "=", "opportunity"],
        ["date_closed", ">=", month_start],
    ]
    if user_id:
        won_domain.append(["user_id", "=", user_id])
    won_count = len(
        models.execute_kw(ODOO_DB, uid, ODOO_API_KEY,
            "crm.lead", "search", [won_domain],
            {"limit": 5000, "context": {"active_test": False}})
    )

    new_today_domain = [["create_date", ">=", today_str + " 00:00:00"], ["active", "=", True]]
    if user_id:
        new_today_domain.append(["user_id", "=", user_id])
    new_today = len(xcall("crm.lead", "search", new_today_domain))

    overdue_details = act_records(
        [["date_deadline", "<", today_str]],
        ["res_id", "res_name", "date_deadline", "activity_type_id", "user_id", "summary"],
    )

    by_salesperson = []
    if not user_id:
        tally = {}
        for act in overdue_details:
            u = act.get("user_id")
            if u:
                tally.setdefault(u[0], {"name": u[1], "overdue": 0})
                tally[u[0]]["overdue"] += 1
        by_salesperson = list(tally.values())

    return {
        "date":             today_str,
        "salesperson_id":   user_id,
        "summary": {
            "total_active_leads": total_active,
            "no_activity":        len(no_act_ids),
            "due_today":          due_today,
            "overdue":            overdue,
            "upcoming":           upcoming,
            "won_this_month":     won_count,
            "new_leads_today":    new_today,
        },
        "no_activity_lead_ids": no_act_ids,
        "overdue_activities":   overdue_details,
        "by_salesperson":       by_salesperson,
    }


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        token = (
            params.get("token", [None])[0]
            or self.headers.get("Authorization", "").replace("Bearer ", "").strip()
        )
        if DASHBOARD_TOKEN and token != DASHBOARD_TOKEN:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return

        raw_uid = params.get("salesperson_id", [None])[0]
        user_id = int(raw_uid) if raw_uid else None

        try:
            uid, models = _odoo_connect()
            data = _build_dashboard(uid, models, user_id)
            body = json.dumps(data, indent=2, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        print(f"[dashboard] {self.address_string()} - {format % args}")
