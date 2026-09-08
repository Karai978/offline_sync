from odoo import http #type: ignore
from odoo.http import request #type: ignore
from odoo.exceptions import AccessDenied #type: ignore
import logging
import json

from .common import OfflineSyncMixin

_logger = logging.getLogger(__name__)


class AuthController(http.Controller, OfflineSyncMixin):

    @http.route(
            "/offline_sync/ping", 
            type="http", 
            auth="none",
            methods=["GET", "OPTIONS"], 
            csrf=False
        )
    def ping(self, **kwargs):
        """Ultra-lightweight, unauthenticated route used 
        solely to verify actual reachability of the Odoo server 
        (beyond `navigator.onLine`, which only detects the local 
        network interface status, not actual server access).."""

        if request.httprequest.method == "OPTIONS":
            return self._cors_response()
        return self._cors_response(json.dumps({"status": "ok"}))

    @http.route(
            "/offline_sync/login", 
            type="http", 
            auth="none",
            methods=["POST", "OPTIONS"], 
            csrf=False
        )
    def login(self, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return self._cors_response()

        try:
            body = json.loads(request.httprequest.data)
            login = body.get("login")
            password = body.get("password")
        except Exception:
            return self._cors_response(json.dumps({"error": "Invalid request"}), status=400)

        if not login or not password:
            return self._cors_response(
                json.dumps({"error": "Email and password required"}), status=400
            )

        db = request.env.cr.dbname
        try:
            uid = request.env["res.users"]._login(db, login, password, {"interactive": False})
        except AccessDenied:
            uid = False
        except Exception:
            _logger.exception("Authentication error")
            uid = False

        if not uid:
            return self._cors_response(
                json.dumps({"error": "Incorrect email or password"}), status=401
            )

        user = request.env["res.users"].sudo().browse(uid)
        if not user.offline_sync_api_key:
            user.action_generate_offline_sync_key()

        return self._cors_response(json.dumps({
            "uid": user.id,
            "name": user.name,
            "api_key": user.offline_sync_api_key,
        }))