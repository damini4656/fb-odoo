import os
import json
import httpx
import xmlrpc.client
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "").strip()
PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN", "").strip()
ODOO_URL = os.environ.get("ODOO_URL", "")
ODOO_DB = os.environ.get("ODOO_DB", "")
ODOO_USERNAME = os.environ.get("ODOO_USERNAME", "")
ODOO_API_KEY = os.environ.get("ODOO_API_KEY", "")


def _fetch_lead(leadgen_id: str) -> dict:
    url = f"https://graph.facebook.com/{leadgen_id}"
    r = httpx.get(url, params={"fields": "field_data,created_time,ad_id,form_id", "access_token": PAGE_ACCESS_TOKEN})
    r.raise_for_status()
    return r.json()


def _extract_fields(lead_data: dict) -> dict:
    fields = {}
    for item in lead_data.get("field_data", []):
        key = item.get("name", "").lower()
        values = item.get("values", [])
        fields[key] = values[0] if values else ""
    return fields


def _push_to_odoo(fields: dict, leadgen_id: str) -> int:
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_API_KEY, {})
    if not uid:
        raise ValueError("Odoo authentication failed")

    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

    name_parts = [
        fields.get("full_name", ""),
        fields.get("first_name", ""),
        fields.get("last_name", ""),
    ]
    contact_name = next((p for p in name_parts if p), "Facebook Lead")

    lead_id = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "create",
        [{
            "name": f"FB Lead - {contact_name}",
            "contact_name": contact_name,
            "email_from": fields.get("email", ""),
            "phone": fields.get("phone_number", fields.get("phone", "")),
            "description": f"Facebook Lead ID: {leadgen_id}\n\nAll fields:\n" +
                           "\n".join(f"{k}: {v}" for k, v in fields.items()),
            "source_id": False,
        }]
    )

    if lead_id:
        try:
            import sys as _sys, os as _os
            _sys.path.insert(0, _os.path.dirname(__file__))
            from odoo_activities import schedule_activity
            schedule_activity(uid, models, lead_id)
        except Exception as _e:
            print(f"[webhook] activity scheduling failed: {_e}")

    return lead_id


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        mode = params.get("hub.mode", [None])[0]
        token = params.get("hub.verify_token", [None])[0]
        challenge = params.get("hub.challenge", [None])[0]

        if mode == "subscribe" and token == VERIFY_TOKEN:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(challenge.encode())
        else:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            payload = json.loads(body)
            entry = payload.get("entry", [{}])[0]
            change = entry.get("changes", [{}])[0]
            value = change.get("value", {})
            leadgen_id = value.get("leadgen_id")

            if not leadgen_id:
                self._respond(200, "no leadgen_id, skipping")
                return

            lead_data = _fetch_lead(leadgen_id)
            fields = _extract_fields(lead_data)
            odoo_id = _push_to_odoo(fields, leadgen_id)

            self._respond(200, json.dumps({"ok": True, "odoo_lead_id": odoo_id}))

        except Exception as e:
            print(f"[webhook] error: {e}")
            self._respond(200, json.dumps({"ok": False, "error": str(e)}))

    def _respond(self, status: int, body: str):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format, *args):
        print(f"[webhook] {self.address_string()} - {format % args}")
