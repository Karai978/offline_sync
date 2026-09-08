from odoo import http #type: ignore
from odoo.http import request #type: ignore
import logging
import json
import base64
import os

from .common import OfflineSyncMixin
from ..utils.app_discovery import guess_main_model

_logger = logging.getLogger(__name__)


class AppsController(http.Controller, OfflineSyncMixin):

    """Retrieve the installed Odoo applications to build the PWA dashboard/menu."""

    @http.route("/offline_sync/installed_apps", type="http", auth="none",
            methods=["GET", "OPTIONS"], csrf=False)
    def installed_apps(self, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return self._cors_response()

        user = self._authenticate_api_key()
        if not user:
            return self._cors_response(json.dumps({"error": "Invalid API Key"}), status=401)

        env = request.env(user=user.id)

        apps = env["ir.module.module"].sudo().search([
            ("application", "=", True),
            ("state", "=", "installed"),
        ])

        result = []
        for app in apps:
            main_model = guess_main_model(env, app.name, self._is_technical_model)
            icon_base64 = self._get_icon_base64(app)
            menus = self._get_app_menus(env, app.name)

            result.append({
                "technical_name": app.name,
                "label": app.shortdesc or app.name,
                "icon_base64": icon_base64,
                "main_model": main_model,
                "menus": menus,
            })

        return self._cors_response(json.dumps({"apps": result}))

    def _get_app_menus(self, env, module_name):
        """Menus appartenant au module (et à ses dépendances) qui sont
        RÉELLEMENT visibles pour l'utilisateur courant. Le filtrage de
        visibilité est délégué à OfflineSyncMixin._get_visible_menu_objs()
        (partagé avec metadata_controller.py) — ne pas le recalculer ici."""
        IrModelData = env["ir.model.data"].sudo()
        Module = env["ir.module.module"].sudo()

        module_rec = Module.search([("name", "=", module_name)], limit=1)
        candidate_modules = [module_name]
        if module_rec:
            candidate_modules += module_rec.dependencies_id.mapped("name")

        menu_data = IrModelData.search([
            ("module", "in", candidate_modules),
            ("model", "=", "ir.ui.menu"),
        ])

        all_menu_objs = []
        for data in menu_data:
            menu = env["ir.ui.menu"].sudo().browse(data.res_id)
            if menu.exists():
                all_menu_objs.append(menu)

        visible_menu_objs = self._get_visible_menu_objs(env, all_menu_objs)
        if not visible_menu_objs:
            return []

        visible_ids = {m.id for m in visible_menu_objs}
        menus = sorted(visible_menu_objs, key=lambda m: (m.sequence, m.id))

        return [{
            "id": m.id,
            "name": m.name,
            "parent_id": m.parent_id.id if m.parent_id.id in visible_ids else None,
            "action_model": (
                m.action.res_model
                if m.action and m.action.type == "ir.actions.act_window"
                else None
            ),
        } for m in menus]

    def _get_icon_base64(self, app):
        """Reads the module's icon file from the disk and returns it
        base64-encoded, ready for use as a data URI on the PWA side."""
        if not app.icon:
            return None

        try:
            from odoo.modules.module import get_module_resource #type: ignore

            parts = app.icon.strip("/").split("/", 1)
            if len(parts) != 2:
                return None
            module_name, relative_path = parts

            full_path = get_module_resource(module_name, relative_path)
            if not full_path or not os.path.exists(full_path):
                return None

            with open(full_path, "rb") as f:
                raw = f.read()

            ext = os.path.splitext(full_path)[1].lstrip(".") or "png"
            encoded = base64.b64encode(raw).decode("utf-8")
            return f"data:image/{ext};base64,{encoded}"

        except Exception:
            _logger.warning("Unable to read the icon for the module. %s", app.name, exc_info=True)
            return None