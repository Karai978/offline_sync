from odoo.http import request #type: ignore
import datetime as dt
import functools
import json
import logging
import re

from ..utils.json_utils import json_safe

_logger = logging.getLogger(__name__)

TMP_REF_PATTERN = re.compile(r"^tmp:(.+)$")


def find_tmp_refs(value):
    """find all temporary tmp:UUID references present in a Python structure."""
    refs = set()
    if isinstance(value, str):
        match = TMP_REF_PATTERN.match(value)
        if match:
            refs.add(match.group(1))
    elif isinstance(value, dict):
        for v in value.values():
            refs |= find_tmp_refs(v)
    elif isinstance(value, list):
        for v in value:
            refs |= find_tmp_refs(v)
    return refs


def substitute_tmp_refs(value, uuid_to_id):
    """Recursively replaces any resolved 'tmp:<uuid>' reference with its
    actual Odoo ID. Unresolved references are left
    as-is (the caller must check via find_tmp_refs beforehand)"""
    if isinstance(value, str):
        match = TMP_REF_PATTERN.match(value)
        if match:
            return uuid_to_id.get(match.group(1), value)
        return value
    if isinstance(value, dict):
        return {k: substitute_tmp_refs(v, uuid_to_id) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_tmp_refs(v, uuid_to_id) for v in value]
    return value


class OfflineSyncMixin:

    SUPPORTED_TYPES = {
        "char", "text", "integer", "float", "boolean",
        "selection", "date", "datetime", "many2one", "one2many",
        "monetary", "many2many",
    }

    IGNORED_FIELDS = {
        "id", "create_uid", "write_date", "write_uid", "__last_update",
        "message_follower_ids", "message_ids", "message_main_attachment_id",
        "website_message_ids", "activity_ids", "activity_state", "activity_user_id",
        "activity_type_id", "activity_type_icon", "activity_date_deadline",
        "activity_summary", "activity_exception_decoration", "activity_exception_icon",
        "activity_calendar_event_id", "my_activity_date_deadline",
        "message_has_error", "message_has_error_counter", "message_attachment_count",
        "message_is_follower", "message_partner_ids", "message_needaction",
        "message_needaction_counter", "message_has_sms_error", "has_message",
        "message_unread", "message_unread_counter",
    }

    TECHNICAL_MODEL_PATTERNS = [
        r"\.config\.settings$",
        r"\.template$",
        r"\.tag$",
        r"\.tags$",
        r"\.category$",
        r"\biap\.",
        r"\.attribute$",
        r"\.attribute\.value$",
        r"\.pricelist$",
        r"\.pricelist\.item$",
        r"\.report$",
        r"^res\.company$",
        r"^res\.currency$",
        r"^uom\.",
        r"\.mining\.",
    ]

    TECHNICAL_MODEL_EXCEPTIONS = {
        "product.template",
    }

    def _is_technical_model(self, model_name):
        """Generically detects whether a model is a technical model
        rather than a true business object"""

        if not model_name:
            return False
        if model_name in self.TECHNICAL_MODEL_EXCEPTIONS:
            return False
        return any(re.search(pattern, model_name) for pattern in self.TECHNICAL_MODEL_PATTERNS)

    def _get_allowed_origins(self):
        """Reads the comma-separated whitelist of allowed origins from
        ir.config_parameter (offline_sync.allowed_origins). E.g., 'http://localhost:8080,http://localhost:5500'
  ​​      Returns an empty list if the parameter does not exist or is empty."""
        
        param = request.env["ir.config_parameter"].sudo().get_param(
            "offline_sync.allowed_origins", ""
        )
        if not param:
            return []
        return [o.strip() for o in param.split(",") if o.strip()]

    def _cors_response(self, body="", status=200):
        origin = request.httprequest.headers.get("Origin", "")
        allowed_origins = self._get_allowed_origins()

        response = request.make_response(
            body,
            headers=[("Content-Type", "application/json")] if body else [],
            status=status,
        )

        if not allowed_origins:
            _logger.error(
                "offline_sync.allowed_origins is not configured — "
                "no origin will be allowed until this system parameter "
                "is set (Settings > Technical > System Parameters).."
            )

        elif origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        else:
            _logger.warning(
                "CORS denied for origin '%s' — not present in "
                "offline_sync.allowed_origins (%s)", origin, allowed_origins
            )

        response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return response

    def _authenticate_api_key(self):
        auth_header = request.httprequest.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        api_key = auth_header.replace("Bearer ", "").strip()
        if not api_key:
            return None

        user = request.env["res.users"].sudo().search(
            [("offline_sync_api_key", "=", api_key)], limit=1
        )
        return user or None

    def _json_safe(self, value):
        """Recursively converts non-serializable types (date, datetime) to JSON strings."""
        return json_safe(value)

    def _resolve_action_domain(self, env, model_name, action_id):
        """Combines the action domain and the active default filters
           (context 'search_default_<filter_name>', e.g., the "My Quotes" badge)."""
        from odoo.tools.safe_eval import safe_eval #type: ignore
        from odoo import fields as odoo_fields #type: ignore
        import xml.etree.ElementTree as ET

        domain = []
        if not action_id:
            return domain

        try:
            action = env["ir.actions.act_window"].sudo().browse(int(action_id))
        except (ValueError, TypeError):
            return domain

        if not action.exists():
            return domain

        eval_context = {
            "uid": env.uid,
            "context_today": lambda *a: odoo_fields.Date.context_today(env.user),
        }

        if action.domain:
            try:
                parsed = safe_eval(action.domain, eval_context)
                if isinstance(parsed, list):
                    domain += parsed
            except Exception:
                pass

        action_context = {}
        if action.context:
            try:
                parsed_ctx = safe_eval(action.context, eval_context)
                if isinstance(parsed_ctx, dict):
                    action_context = parsed_ctx
            except Exception:
                action_context = {}

        default_filter_names = [
            key[len("search_default_"):]
            for key, value in action_context.items()
            if key.startswith("search_default_") and value
        ]
        if not default_filter_names:
            return domain

        try:
            if action.search_view_id:
                search_view = env[model_name].sudo().get_view(
                    view_id=action.search_view_id.id, view_type="search"
                )
            else:
                search_view = env[model_name].sudo().get_view(view_type="search")
            arch = ET.fromstring(search_view.get("arch"))
        except Exception:
            return domain

        for filter_node in arch.iter("filter"):
            fname = filter_node.get("name")
            fdomain = filter_node.get("domain")
            if fname in default_filter_names and fdomain:
                try:
                    parsed_fdomain = safe_eval(fdomain, eval_context)
                    if isinstance(parsed_fdomain, list):
                        domain += parsed_fdomain
                except Exception:
                    continue

        return domain

    def _get_visible_menu_objs(self, env, menu_objs):
        """Filtre une liste de recordsets ir.ui.menu pour ne garder que ceux
        réellement visibles par l'utilisateur courant (groups_id sur le menu
        + droits sur le modèle de l'action liée), via le même mécanisme que
        le client web natif d'Odoo. Ne jamais filtrer les menus à la main
        (groups_id) : cette méthode gère aussi l'élagage des parents devenus
        vides et les droits sur l'action, qu'un filtre naïf raterait."""
        visible_ids = set(env["ir.ui.menu"]._visible_menu_ids())
        return [m for m in menu_objs if m.id in visible_ids]