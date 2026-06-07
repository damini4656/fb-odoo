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

ODOO_URL        = os.environ.get("ODOO_URL", "").strip()
ODOO_USERNAME   = os.environ.get("ODOO_USERNAME", "").strip()
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "modglow2026").strip()


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
        if fields:   kwargs["fields"]   = fields
        if context:  kwargs["context"]  = context
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

    # Primary KPIs
    calls_today    = act_count([["date_deadline", "=",  today_str],
                                ["activity_type_id.name", "ilike", "call"]])
    meetings_soon  = act_count([["date_deadline", ">=", today_str],
                                ["activity_type_id.name", "ilike", "meeting"]])
    overdue_all    = act_count([["date_deadline", "<",  today_str]])

    # Secondary stats
    due_today      = act_count([["date_deadline", "=",  today_str]])
    upcoming       = act_count([["date_deadline", ">",  today_str]])
    total_active   = len(xcall("crm.lead", "search", lead_base))
    no_act_ids     = get_leads_without_activity(uid, models, user_id)

    won_domain = [["active","=",False],["type","=","opportunity"],
                  ["date_closed",">=",month_start]]
    if user_id:
        won_domain.append(["user_id","=",user_id])
    won_count = len(models.execute_kw(ODOO_DB, uid, ODOO_API_KEY,
        "crm.lead","search",[won_domain],
        {"limit":5000,"context":{"active_test":False}}))

    new_today_domain = [["create_date",">=",today_str+" 00:00:00"],["active","=",True]]
    if user_id:
        new_today_domain.append(["user_id","=",user_id])
    new_today = len(xcall("crm.lead","search",new_today_domain))

    overdue_details = act_records(
        [["date_deadline","<",today_str]],
        ["res_id","res_name","date_deadline","activity_type_id","user_id","summary"],
    )

    by_salesperson = []
    if not user_id:
        tally = {}
        for act in overdue_details:
            u = act.get("user_id")
            if u:
                tally.setdefault(u[0], {"name": u[1], "overdue": 0})
                tally[u[0]]["overdue"] += 1
        by_salesperson = sorted(tally.values(), key=lambda x: -x["overdue"])

    return {
        "date":           today_str,
        "salesperson_id": user_id,
        "primary": {
            "calls_today":   calls_today,
            "meetings_soon": meetings_soon,
            "overdue":       overdue_all,
        },
        "summary": {
            "total_active_leads": total_active,
            "no_activity":        len(no_act_ids),
            "due_today":          due_today,
            "overdue":            overdue_all,
            "upcoming":           upcoming,
            "won_this_month":     won_count,
            "new_leads_today":    new_today,
        },
        "no_activity_lead_ids": no_act_ids,
        "overdue_activities":   overdue_details,
        "by_salesperson":       by_salesperson,
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>CRM Activity Dashboard</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f0f2f5;color:#1a1a2e}
  .topbar{background:#714b67;color:#fff;padding:14px 28px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10;box-shadow:0 2px 8px rgba(0,0,0,.2)}
  .topbar h1{font-size:18px;font-weight:600;letter-spacing:.3px}
  .topbar .meta{font-size:13px;opacity:.8}
  .topbar .refresh-btn{background:rgba(255,255,255,.15);border:none;color:#fff;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px}
  .topbar .refresh-btn:hover{background:rgba(255,255,255,.25)}
  .container{max-width:1100px;margin:0 auto;padding:24px 20px}
  .section-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#888;margin:24px 0 10px}
  /* Primary KPI cards */
  .primary-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
  .kpi-card{background:#fff;border-radius:12px;padding:24px;box-shadow:0 1px 4px rgba(0,0,0,.08);position:relative;overflow:hidden}
  .kpi-card::before{content:"";position:absolute;top:0;left:0;right:0;height:4px}
  .kpi-card.calls::before{background:#17a2b8}
  .kpi-card.meetings::before{background:#6f42c1}
  .kpi-card.overdue::before{background:#dc3545}
  .kpi-card .label{font-size:13px;color:#666;font-weight:500;margin-bottom:8px}
  .kpi-card .value{font-size:48px;font-weight:700;line-height:1}
  .kpi-card.calls .value{color:#17a2b8}
  .kpi-card.meetings .value{color:#6f42c1}
  .kpi-card.overdue .value{color:#dc3545}
  .kpi-card .sub{font-size:12px;color:#999;margin-top:6px}
  .kpi-card .icon{position:absolute;right:20px;top:50%;transform:translateY(-50%);font-size:36px;opacity:.12}
  /* Secondary stats row */
  .stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
  .stat-box{background:#fff;border-radius:10px;padding:16px 18px;box-shadow:0 1px 3px rgba(0,0,0,.07);display:flex;flex-direction:column;gap:4px}
  .stat-box .s-label{font-size:12px;color:#888;font-weight:500}
  .stat-box .s-value{font-size:26px;font-weight:700;color:#1a1a2e}
  .stat-box.warn .s-value{color:#e67e22}
  .stat-box.good .s-value{color:#27ae60}
  .stat-box.info .s-value{color:#2980b9}
  /* Table */
  .table-card{background:#fff;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.08);overflow:hidden}
  table{width:100%;border-collapse:collapse;font-size:13px}
  thead tr{background:#f8f9fa}
  th{padding:10px 14px;text-align:left;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#888;border-bottom:1px solid #e9ecef}
  td{padding:11px 14px;border-bottom:1px solid #f0f2f5;vertical-align:middle}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:#fafafa}
  .badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
  .badge.overdue{background:#fde8e8;color:#c0392b}
  .badge.type{background:#e8f4f8;color:#2980b9}
  .person{display:flex;align-items:center;gap:6px}
  .avatar{width:26px;height:26px;border-radius:50%;background:#714b67;color:#fff;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;flex-shrink:0}
  /* Salesperson table */
  .sp-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .empty{text-align:center;padding:32px;color:#bbb;font-size:14px}
  .footer{text-align:center;font-size:12px;color:#bbb;padding:20px 0 8px}
  @media(max-width:700px){
    .primary-grid,.stats-grid,.sp-grid{grid-template-columns:1fr 1fr}
    .primary-grid{grid-template-columns:1fr}
  }
</style>
</head>
<body>
<div class="topbar">
  <h1>&#128200; CRM Activity Dashboard</h1>
  <div style="display:flex;align-items:center;gap:16px">
    <span class="meta" id="updated">Loading...</span>
    <button class="refresh-btn" onclick="location.reload()">&#8635; Refresh</button>
  </div>
</div>

<div class="container">

  <div class="section-label">Today's Activity</div>
  <div class="primary-grid">
    <div class="kpi-card calls">
      <div class="label">Today's Calls</div>
      <div class="value">__CALLS_TODAY__</div>
      <div class="sub">Phone calls scheduled for today</div>
      <div class="icon">&#128222;</div>
    </div>
    <div class="kpi-card meetings">
      <div class="label">Upcoming Meetings</div>
      <div class="value">__MEETINGS_SOON__</div>
      <div class="sub">Meetings today &amp; ahead</div>
      <div class="icon">&#128197;</div>
    </div>
    <div class="kpi-card overdue">
      <div class="label">Overdue Follow-ups</div>
      <div class="value">__OVERDUE__</div>
      <div class="sub">Activities past their deadline</div>
      <div class="icon">&#9888;</div>
    </div>
  </div>

  <div class="section-label">Pipeline Overview</div>
  <div class="stats-grid">
    <div class="stat-box info">
      <div class="s-label">Active Leads</div>
      <div class="s-value">__TOTAL_ACTIVE__</div>
    </div>
    <div class="stat-box warn">
      <div class="s-label">No Activity</div>
      <div class="s-value">__NO_ACTIVITY__</div>
    </div>
    <div class="stat-box good">
      <div class="s-label">Won This Month</div>
      <div class="s-value">__WON_MONTH__</div>
    </div>
    <div class="stat-box info">
      <div class="s-label">New Leads Today</div>
      <div class="s-value">__NEW_TODAY__</div>
    </div>
  </div>

  __OVERDUE_SECTION__
  __SP_SECTION__

</div>

<div class="footer">Auto-refreshes every 5 min &nbsp;|&nbsp; __DATE__</div>

<script>
  document.getElementById("updated").textContent = "Updated " + new Date().toLocaleTimeString();
  setTimeout(() => location.reload(), 300000);
</script>
</body>
</html>"""


def _render_html(data):
    p = data["primary"]
    s = data["summary"]

    # Overdue table
    overdue_rows = ""
    for act in data["overdue_activities"][:30]:
        name     = act.get("res_name") or "—"
        deadline = act.get("date_deadline") or "—"
        atype    = act.get("activity_type_id")
        atype_name = atype[1] if isinstance(atype, list) else "—"
        user     = act.get("user_id")
        uname    = user[1] if isinstance(user, list) else "—"
        initials = "".join(w[0].upper() for w in uname.split()[:2]) if uname != "—" else "?"
        summary  = act.get("summary") or ""
        overdue_rows += f"""
        <tr>
          <td><b>{name}</b>{"<br><span style='color:#aaa;font-size:12px'>" + summary + "</span>" if summary else ""}</td>
          <td><span class="badge type">{atype_name}</span></td>
          <td><span class="badge overdue">{deadline}</span></td>
          <td><div class="person"><div class="avatar">{initials}</div>{uname}</div></td>
        </tr>"""

    overdue_section = ""
    if overdue_rows:
        overdue_section = f"""
  <div class="section-label">Overdue Activities ({len(data["overdue_activities"])})</div>
  <div class="table-card">
    <table>
      <thead><tr>
        <th>Lead</th><th>Type</th><th>Deadline</th><th>Assigned To</th>
      </tr></thead>
      <tbody>{overdue_rows}</tbody>
    </table>
  </div>"""
    else:
        overdue_section = """
  <div class="section-label">Overdue Activities</div>
  <div class="table-card"><div class="empty">&#10003; No overdue activities</div></div>"""

    # Salesperson breakdown
    sp_rows = ""
    for sp in data["by_salesperson"]:
        initials = "".join(w[0].upper() for w in sp["name"].split()[:2])
        sp_rows += f"""
        <tr>
          <td><div class="person"><div class="avatar">{initials}</div>{sp["name"]}</div></td>
          <td><span class="badge overdue">{sp["overdue"]}</span></td>
        </tr>"""

    sp_section = ""
    if sp_rows:
        sp_section = f"""
  <div class="section-label">Overdue by Salesperson</div>
  <div class="table-card">
    <table>
      <thead><tr><th>Salesperson</th><th>Overdue</th></tr></thead>
      <tbody>{sp_rows}</tbody>
    </table>
  </div>"""

    html = HTML_TEMPLATE
    html = html.replace("__CALLS_TODAY__",   str(p["calls_today"]))
    html = html.replace("__MEETINGS_SOON__", str(p["meetings_soon"]))
    html = html.replace("__OVERDUE__",       str(p["overdue"]))
    html = html.replace("__TOTAL_ACTIVE__",  str(s["total_active_leads"]))
    html = html.replace("__NO_ACTIVITY__",   str(s["no_activity"]))
    html = html.replace("__WON_MONTH__",     str(s["won_this_month"]))
    html = html.replace("__NEW_TODAY__",     str(s["new_leads_today"]))
    html = html.replace("__OVERDUE_SECTION__", overdue_section)
    html = html.replace("__SP_SECTION__",      sp_section)
    html = html.replace("__DATE__",           data["date"])
    return html


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

        want_json = (
            "json" in params.get("format", [""])[0]
            or "application/json" in self.headers.get("Accept", "")
        )

        try:
            uid, models = _odoo_connect()
            data = _build_dashboard(uid, models, user_id)

            if want_json:
                body = json.dumps(data, indent=2, default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                body = _render_html(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"<pre>Error: {e}</pre>".encode())

    def log_message(self, format, *args):
        print(f"[dashboard] {self.address_string()} - {format % args}")
