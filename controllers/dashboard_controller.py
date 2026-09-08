from odoo import http #type: ignore
from odoo.http import request #type: ignore
import json

from .common import OfflineSyncMixin


class DashboardController(http.Controller, OfflineSyncMixin):

    @http.route(
        "/offline_sync/dashboard_info",
        type="http", 
        auth="none",
        methods=["GET", "OPTIONS"], 
        csrf=False)
    def dashboard_info(self, **kwargs):
        """Retrieve the key information needed for the PWA dashboard."""
        if request.httprequest.method == "OPTIONS":
            return self._cors_response()

        user = self._authenticate_api_key()
        if not user:
            return self._cors_response(
                json.dumps({"error": "API Key Invalid"}), status=401
            )

        unread_count = request.env["mail.notification"].sudo().search_count([
            ("res_partner_id", "=", user.partner_id.id),
            ("is_read", "=", False),
        ])

        activity_count = request.env["mail.activity"].sudo().search_count([
            ("user_id", "=", user.id),
        ])

        return self._cors_response(json.dumps({
            "name": user.name,
            "initial": (user.name or "?")[0].upper(),
            "unread_messages": unread_count,
            "pending_activities": activity_count,
            "company_name": user.company_id.name,
        }))

    @http.route(
        "/offline_sync/app_info",
        type="http",
        auth="none",
        methods=["GET", "OPTIONS"],
        csrf=False)
    def app_info(self, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return self._cors_response()

        user = self._authenticate_api_key()
        if not user:
            return self._cors_response(
                json.dumps({"error": "API Key Invalid"}), status=401
            )

        return self._cors_response(
            json.dumps({"connected_as": user.name, "uid": user.id})
        )

    @http.route(
        "/offline_sync/companies",
        type="http", 
        auth="none",
        methods=["GET", "OPTIONS"],
        csrf=False)
    def companies(self, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return self._cors_response()
        user = self._authenticate_api_key()
        if not user:
            return self._cors_response(json.dumps({"error": "API Key Invalid"}), status=401)

        return self._cors_response(json.dumps({
            "companies": [{"id": c.id, "name": c.name} for c in user.sudo().company_ids],
            "current_company_id": user.company_id.id,
        }))