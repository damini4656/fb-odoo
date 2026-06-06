import os
import json
import httpx
import xmlrpc.client
from http.server import BaseHTTPRequestHandler

USER_ACCESS_TOKEN = os.environ.get("USER_ACCESS_TOKEN", "").strip()
BUSINESS_ID = os.environ.get("BUSINESS_ID", "").strip()
ODOO_URL = os.environ.get("ODOO_URL", "").strip()
ODOO_DB = os.environ.get("ODOO_DB", "").strip()
ODOO_USERNAME = os.environ.get("ODOO_USERNAME", "").strip()
ODOO_API_KEY = os.environ.get("ODOO_API_KEY", "").strip()


def _get_all_pages():
    seen = {}
    # personal pages
    r = httpx.get("https://graph.facebook.com/me/accounts", params={"access_token": USER_ACCESS_TOKEN, "fields": "id,name,access_token", "limit": 100})
    for p in r.json().get("data", []):
        seen[p["id"]] = p

    # business pages
    if BUSINESS_ID:
        r2 = httpx.get(f"https://graph.facebook.com/{BUSINESS_ID}/owned_pages", params={"access_token": USER_ACCESS_TOKEN, "fields": "id,name", "limit": 100})
        for p in r2.json().get("data", []):
            if p["id"] not in seen:
                # get page token
                pt = httpx.get(f"https://graph.facebook.com/{p['id']}", params={"access_token": USER_ACCESS_TOKEN, "fields": "access_token"}).json()
                p["access_token"] = pt.get("access_token", "")
                seen[p["id"]] = p

    return list(seen.values())


def _get_lead_forms(page_id, page_token):
    forms = []
    url = f"https://graph.facebook.com/v19.0/{page_id}/leadgen_forms"
    params = {"access_token": page_token, "fields": "id,name", "limit": 100}
    while True:
        r = httpx.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        forms.extend(data.get("data", []))
        after = data.get("paging", {}).get("cursors", {}).get("after")
        if not after or not data.get("data"):
            break
        params = {"access_token": page_token, "fields": "id,name", "limit": 100, "after": after}
    return forms


def _get_leads_from_form(form_id, page_token):
    leads = []
    url = f"https://graph.facebook.com/v19.0/{form_id}/leads"
    params = {"access_token": page_token, "fields": "id,field_data,created_time", "limit": 100}
    while True:
        r = httpx.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        leads.extend(data.get("data", []))
        after = data.get("paging", {}).get("cursors", {}).get("after")
        if not after or not data.get("data"):
            break
        params = {"access_token": page_token, "fields": "id,field_data,created_time", "limit": 100, "after": after}
    return leads


def _extract_fields(field_data):
    fields = {}
    for item in field_data:
        key = item.get("name", "").lower()
        values = item.get("values", [])
        fields[key] = values[0] if values else ""
    return fields


def _label(key):
    return key.replace("_", " ").replace("?", "").strip().title()


def _format_value(v):
    return v.replace("_", " ").strip() if isinstance(v, str) else v


CORE_FIELDS = {"full_name", "first_name", "last_name", "email", "phone",
               "phone_number", "street_address", "address", "city", "zip",
               "postal_code", "pin_code"}


def _build_description(fields, leadgen_id, page_name, form_name):
    address = fields.get("street_address", fields.get("address", ""))
    city = fields.get("city", "")
    full_address = ", ".join(p for p in [address, city] if p)

    extra = {k: v for k, v in fields.items() if k not in CORE_FIELDS and v}

    def row(label, value, highlight=False):
        style = "background:#1877F220;font-weight:600" if highlight else ""
        return (
            f"<tr style='{style}'>"
            f"<td style='padding:6px 16px 6px 8px;color:#888;white-space:nowrap;width:160px'><b>{label}</b></td>"
            f"<td style='padding:6px 0'>{value}</td></tr>"
        )

    rows = ""
    if full_address:
        rows += row("Address", full_address, highlight=True)
    for k, v in extra.items():
        rows += row(_label(k), _format_value(v))

    details_section = (
        f"<p style='margin:12px 0 6px'><b>Details</b></p>"
        f"<table style='border-collapse:collapse;font-size:14px;width:100%;border:1px solid #e0e0e0;border-radius:6px'>{rows}</table>"
        if rows else ""
    )

    return (
        f"<div style='font-family:sans-serif;font-size:14px'>"
        f"<p style='margin:0 0 8px'>"
        f"<span style='background:#1877F2;color:#fff;padding:2px 8px;border-radius:4px;font-size:12px'>Facebook Lead</span>"
        f"&nbsp;<span style='color:#888;font-size:12px'>ID: {leadgen_id}</span></p>"
        f"<p style='margin:0 0 4px;color:#888;font-size:12px'>Page: <b>{page_name}</b> &nbsp;|&nbsp; Form: <b>{form_name}</b></p>"
        f"{details_section}"
        f"</div>"
    )


def _odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_API_KEY, {})
    if not uid:
        raise ValueError("Odoo authentication failed")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def _lead_exists(uid, models, leadgen_id):
    results = models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "search",
        [[["description", "like", f"Facebook Lead ID: {leadgen_id}"]]]
    )
    return len(results) > 0


def _push_lead(uid, models, fields, leadgen_id, page_name, form_name):
    if _lead_exists(uid, models, leadgen_id):
        return None

    name_candidates = [
        fields.get("full_name", ""),
        (fields.get("first_name", "") + " " + fields.get("last_name", "")).strip(),
    ]
    contact_name = next((n for n in name_candidates if n), "Facebook Lead")

    street = fields.get("street_address", fields.get("address", ""))
    city = fields.get("city", "")
    zip_code = fields.get("zip", fields.get("postal_code", fields.get("pin_code", "")))

    return models.execute_kw(
        ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead", "create",
        [{
            "name": f"FB Lead - {contact_name}",
            "contact_name": contact_name,
            "email_from": fields.get("email", ""),
            "phone": fields.get("phone_number", fields.get("phone", "")),
            "street": street,
            "city": city,
            "zip": zip_code,
            "description": _build_description(fields, leadgen_id, page_name, form_name),
        }]
    )


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        results = {"pages": 0, "forms": 0, "synced": [], "skipped": 0, "errors": []}

        try:
            uid, models = _odoo_connect()
            pages = _get_all_pages()
            results["pages"] = len(pages)

            for page in pages:
                page_id = page["id"]
                page_name = page["name"]
                page_token = page["access_token"]

                try:
                    forms = _get_lead_forms(page_id, page_token)
                    results["forms"] += len(forms)

                    for form in forms:
                        form_id = form["id"]
                        form_name = form.get("name", form_id)

                        try:
                            leads = _get_leads_from_form(form_id, page_token)
                            for lead in leads:
                                leadgen_id = lead["id"]
                                try:
                                    fields = _extract_fields(lead.get("field_data", []))
                                    odoo_id = _push_lead(uid, models, fields, leadgen_id, page_name, form_name)
                                    if odoo_id:
                                        results["synced"].append({
                                            "leadgen_id": leadgen_id,
                                            "page": page_name,
                                            "form": form_name,
                                            "odoo_lead_id": odoo_id,
                                        })
                                    else:
                                        results["skipped"] += 1
                                except Exception as e:
                                    results["errors"].append({"leadgen_id": leadgen_id, "error": str(e)})
                        except Exception as e:
                            results["errors"].append({"form_id": form_id, "error": str(e)})
                except Exception as e:
                    results["errors"].append({"page_id": page_id, "error": str(e)})

        except Exception as e:
            results["errors"].append({"error": str(e)})

        body = json.dumps(results, indent=2)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format, *args):
        print(f"[poll] {self.address_string()} - {format % args}")
