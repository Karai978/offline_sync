from odoo import http #type: ignore
from odoo.http import request #type: ignore
import json

from .common import OfflineSyncMixin
from ..utils.app_discovery import guess_main_model


class FieldsController(http.Controller, OfflineSyncMixin):
    """Introspection de champs et de droits d'accès pour un modèle donné,
    à la demande — indépendant de la description complète d'une app (voir
    metadata_controller.py, qui reste la source la plus complète pour les
    champs). Séparé car c'est un usage différent : ici on interroge UN
    modèle isolé, pas toute une app d'un coup."""

    # Volontairement plus restreint que OfflineSyncMixin.SUPPORTED_TYPES
    # (pas de relationnel) — cette route sert des besoins plus légers
    # que module_manifest(), qui reste la source complète des champs
    # (avec many2one/one2many/many2many inclus).
    SIMPLE_FIELD_TYPES = {"char", "text", "integer", "float", "boolean", "selection", "date", "datetime"}

    TECHNICAL_FIELDS = ("id", "create_date", "create_uid", "write_date", "write_uid", "__last_update")

    @http.route("/offline_sync/model_fields", type="http", auth="none",
            methods=["GET", "OPTIONS"], csrf=False)
    def model_fields(self, model=None, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return self._cors_response()

        user = self._authenticate_api_key()
        if not user:
            return self._cors_response(json.dumps({"error": "Clé API invalide"}), status=401)

        if not model or model not in request.env:
            return self._cors_response(json.dumps({"error": "Modèle invalide"}), status=400)

        env = request.env(user=user.id)
        result = self._get_fields_info(env, model)

        return self._cors_response(json.dumps({"model": model, "fields": result}))

    @http.route("/offline_sync/security_info", type="http", auth="none",
            methods=["GET", "OPTIONS"], csrf=False)
    def security_info(self, model=None, models=None, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return self._cors_response()

        user = self._authenticate_api_key()
        if not user:
            return self._cors_response(json.dumps({"error": "Clé API invalide"}), status=401)

        env = request.env(user=user.id)

        if model:
            target_models = [model]
        elif models:
            target_models = [m.strip() for m in models.split(",") if m.strip()]
        else:
            target_models = self._get_synced_models(env)

        target_models = [m for m in target_models if m in env]

        models_info = {}
        for model_name in target_models:
            Model = env[model_name]

            access = {
                "read": Model.check_access_rights("read", raise_exception=False),
                "write": Model.check_access_rights("write", raise_exception=False),
                "create": Model.check_access_rights("create", raise_exception=False),
                "unlink": Model.check_access_rights("unlink", raise_exception=False),
            }

            # FIX : pas de .sudo() ici. sudo() force env.su = True, et
            # ir.rule._compute_domain() court-circuite le calcul et
            # renvoie [] (= aucune restriction) dès que env.su est vrai.
            # On veut le domaine réel résolu pour CET utilisateur.
            domain = env["ir.rule"]._compute_domain(model_name, "read") or []

            fields_info = self._get_fields_info(env, model_name)

            models_info[model_name] = {
                "access": access,
                "domain": domain,
                "fields": fields_info,
            }

        return self._cors_response(json.dumps({
            "user_id": user.id,
            "groups": [g.name for g in user.groups_id],
            "is_admin": user.has_group("base.group_system"),
            "models": models_info,
        }))

    def _get_fields_info(self, env, model_name):
        """Champs simples (non-relationnels) d'un modèle, déjà filtrés
        selon les droits par groupe de l'utilisateur porté par env
        (fields_get() applique automatiquement field.groups= via
        user_has_groups en interne — pas besoin de refiltrer à la main)."""
        Model = env[model_name]
        fields_info = Model.fields_get()

        result = []
        for fname, finfo in fields_info.items():
            if fname in self.TECHNICAL_FIELDS:
                continue
            if finfo.get("type") not in self.SIMPLE_FIELD_TYPES:
                continue
            result.append({
                "name": fname,
                "label": finfo.get("string", fname),
                "type": finfo.get("type"),
                "required": finfo.get("required", False),
                "readonly": finfo.get("readonly", False),
                "selection": finfo.get("selection") if finfo.get("type") == "selection" else None,
            })
        return result

    def _get_synced_models(self, env):
        """Repli utilisé UNIQUEMENT quand security_info() est appelée sans
        aucun paramètre ('model' ni 'models') — cas du login initial, avant
        que l'utilisateur ait téléchargé une app précise (voir login.js).
        Dérivé dynamiquement du modèle principal de chaque app installée
        (guess_main_model, déjà utilisé par apps_controller.py) plutôt
        qu'une liste codée en dur limitée à Sales. Ne couvre qu'un seul
        modèle par app (pas les sous-modèles comme sale.order.line) — ce
        repli est transitoire : dès qu'une app est téléchargée via
        downloadFullApp(), security_info() est rappelée avec la liste
        complète issue de module_manifest(), qui elle inclut les
        sous-modèles."""
        apps = env["ir.module.module"].sudo().search([
            ("application", "=", True),
            ("state", "=", "installed"),
        ])

        models = set()
        for app in apps:
            main_model = guess_main_model(env, app.name, self._is_technical_model)
            if main_model:
                models.add(main_model)

        return list(models)