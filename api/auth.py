import os
import httpx
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

APP_ID = os.environ.get("APP_ID", "").strip()
APP_SECRET = os.environ.get("APP_SECRET", "").strip()
REDIRECT_URI = os.environ.get("REDIRECT_URI", "").strip()

SCOPES = "leads_retrieval,pages_manage_ads,pages_manage_metadata,business_management,pages_show_list"


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        # Step 2: Facebook redirects back with ?code=...
        if "code" in params:
            code = params["code"][0]

            # Exchange code for short-lived token
            r = httpx.get("https://graph.facebook.com/oauth/access_token", params={
                "client_id": APP_ID,
                "client_secret": APP_SECRET,
                "redirect_uri": REDIRECT_URI,
                "code": code,
            })
            short = r.json()
            if "error" in short:
                self._html(f"<h2>Error</h2><pre>{short}</pre>")
                return

            short_token = short["access_token"]

            # Exchange for 60-day long-lived user token
            r2 = httpx.get("https://graph.facebook.com/oauth/access_token", params={
                "grant_type": "fb_exchange_token",
                "client_id": APP_ID,
                "client_secret": APP_SECRET,
                "fb_exchange_token": short_token,
            })
            long_lived = r2.json()
            if "error" in long_lived:
                self._html(f"<h2>Error exchanging token</h2><pre>{long_lived}</pre>")
                return

            user_token = long_lived["access_token"]

            # Get permanent page tokens
            r3 = httpx.get("https://graph.facebook.com/me/accounts", params={
                "access_token": user_token,
                "fields": "id,name,access_token",
                "limit": 100,
            })
            pages = r3.json().get("data", [])

            page_rows = "".join(
                f"<tr><td style='padding:8px;border:1px solid #ddd'><b>{p['name']}</b><br><small>{p['id']}</small></td>"
                f"<td style='padding:8px;border:1px solid #ddd;font-family:monospace;font-size:11px;word-break:break-all'>{p['access_token']}</td></tr>"
                for p in pages
            )

            self._html(f"""
                <h2 style='color:green'>Tokens Generated Successfully</h2>
                <p>Copy these into your Vercel environment variables:</p>

                <h3>USER_ACCESS_TOKEN (60 days)</h3>
                <textarea rows='4' style='width:100%;font-family:monospace;font-size:11px'>{user_token}</textarea>

                <h3>PAGE_ACCESS_TOKEN (never expires)</h3>
                <p>Pick the page you use for lead ads:</p>
                <table style='border-collapse:collapse;width:100%'>{page_rows}</table>

                <hr>
                <p style='color:#888;font-size:12px'>
                    The USER_ACCESS_TOKEN refreshes every 60 days — revisit
                    <b>/auth</b> to renew it.<br>
                    Page tokens derived from a long-lived token <b>never expire</b>.
                </p>
            """)
            return

        # Step 1: redirect to Facebook OAuth
        oauth_url = "https://www.facebook.com/v19.0/dialog/oauth?" + urlencode({
            "client_id": APP_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "response_type": "code",
        })
        self.send_response(302)
        self.send_header("Location", oauth_url)
        self.end_headers()

    def _html(self, body):
        html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<style>body{{font-family:sans-serif;max-width:900px;margin:40px auto;padding:20px}}
textarea{{width:100%;padding:8px;font-size:11px}}
h3{{margin-top:24px}}</style>
</head><body>{body}</body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def log_message(self, format, *args):
        pass
