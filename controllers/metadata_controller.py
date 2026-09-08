from odoo import http #type: ignore
from odoo.http import request #type: ignore

import logging
import json

from .common import OfflineSyncMixin
from ..utils.app_discovery import guess_main_model

_logger = logging.getLogger(__name__)


class MetadataController(http.Controller, OfflineSyncMixin):
    """Décrit la structure complète d'UNE app Odoo à la fois (menus, vues,
    champs) — c'est ce payload qui permet à la PWA de générer dynamiquement
    ses écrans sans jamais coder "en dur" la structure d'une app précise.

    Pour la liste des apps installées, voir apps_controller.py.
    Pour l'introspection ponctuelle de champs/droits d'un modèle isolé,
    voir fields_controller.py. Ces 3 sujets vivaient avant dans un seul
    fichier — séparés car ce sont 3 besoins différents côté PWA."""

    @http.route("/offline_sync/module_manifest", type="http", auth="none",
                methods=["GET", "OPTIONS"], csrf=False)
    def module_manifest(self, module=None, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return self._cors_response()

        user = self._authenticate_api_key()
        if not user:
            return self._cors_response(json.dumps({"error": "Clé API invalide"}), status=401)

        if not module:
            return self._cors_response(json.dumps({"error": "Paramètre 'module' requis"}), status=400)

        env = request.env(user=user.id)

        module_rec = env["ir.module.module"].sudo().search(
            [("name", "=", module), ("state", "=", "installed")], limit=1
        )
        if not module_rec:
            return self._cors_response(
                json.dumps({"error": f"Module '{module}' non trouvé ou non installé"}), status=404
            )

        IrModelData = env["ir.model.data"].sudo()

        Module = env["ir.module.module"].sudo()
        module_rec_for_deps = Module.search([("name", "=", module)], limit=1)
        candidate_modules = [module]
        if module_rec_for_deps:
            candidate_modules += module_rec_for_deps.dependencies_id.mapped("name")

        menu_data = IrModelData.search([
            ("module", "in", candidate_modules),
            ("model", "=", "ir.ui.menu"),
        ])

        all_menu_objs = []
        for data in menu_data:
            menu = env["ir.ui.menu"].sudo().browse(data.res_id)
            if menu.exists():
                all_menu_objs.append(menu)

        all_menu_objs = self._get_visible_menu_objs(env, all_menu_objs)

        main_model_hint = guess_main_model(env, module, self._is_technical_model)

        root_menus = [m for m in all_menu_objs if not m.parent_id]

        def subtree_contains_model(root, target_model, all_objs):
            """Vérifie si le modèle cible apparaît dans le sous-arbre de root."""
            ids_in_subtree = {root.id}
            changed = True
            while changed:
                changed = False
                for m in all_objs:
                    if m.parent_id and m.parent_id.id in ids_in_subtree and m.id not in ids_in_subtree:
                        ids_in_subtree.add(m.id)
                        changed = True
            for m in all_objs:
                if m.id in ids_in_subtree and m.action and m.action.type == "ir.actions.act_window":
                    action = env["ir.actions.act_window"].sudo().browse(m.action.id)
                    if action.res_model == target_model:
                        return True
            return False

        selected_root = None
        if main_model_hint:
            for root in root_menus:
                if subtree_contains_model(root, main_model_hint, all_menu_objs):
                    selected_root = root
                    break

        if selected_root:
            valid_ids = {selected_root.id}
            changed = True
            while changed:
                changed = False
                for m in all_menu_objs:
                    if m.parent_id and m.parent_id.id in valid_ids and m.id not in valid_ids:
                        valid_ids.add(m.id)
                        changed = True
            filtered_menu_objs = [m for m in all_menu_objs if m.id in valid_ids]
        else:
            filtered_menu_objs = all_menu_objs

        menus = []
        models_used = set()

        for menu in filtered_menu_objs:
            model_name = None
            action_id = None

            if menu.action and menu.action.type == "ir.actions.act_window":
                action = env["ir.actions.act_window"].sudo().browse(menu.action.id)
                model_name = action.res_model
                action_id = action.id

            elif menu.action and menu.action.type == "ir.actions.server":
                # Certains menus (ex: "Mon Pipeline" du CRM) pointent vers une
                # action serveur qui, à l'exécution, renvoie l'action réelle
                # à afficher — souvent une VRAIE action ir.actions.act_window
                # déjà stockée en base (avec son propre id, comme le "320"
                # visible dans l'URL du vrai Odoo), qu'on traite alors comme
                # n'importe quelle autre action normale (mêmes vues/domaine
                # par action déjà gérés plus bas dans cette fonction).
                resolved = None
                try:
                    with env.cr.savepoint():
                        result = env["ir.actions.server"].sudo().browse(menu.action.id).run()
                        resolved = result
                        raise ValueError("rollback volontaire — lecture seule")
                except ValueError:
                    pass
                except Exception as e:
                    _logger.info("Action serveur du menu %s non résolue: %s", menu.name, e)

                if isinstance(resolved, dict):
                    resolved_id = resolved.get("id")
                    resolved_type = resolved.get("type")
                    if resolved_id and resolved_type == "ir.actions.act_window":
                        real_action = env["ir.actions.act_window"].sudo().browse(resolved_id)
                        if real_action.exists():
                            model_name = real_action.res_model
                            action_id = real_action.id
                    elif resolved.get("res_model"):
                        # Action ad-hoc sans id stable — on ne récupère que le
                        # modèle, pas de vue/domaine spécifique possible.
                        model_name = resolved.get("res_model")
                        action_id = None

            if model_name and not self._is_technical_model(model_name):
                models_used.add(model_name)

            # Type de vue à ouvrir en premier (ex: Kanban pour un pipeline),
            # déduit de action.view_mode ("kanban,form,..." -> "kanban").
            default_view = None
            menu_domain = None
            if action_id:
                act_rec = env["ir.actions.act_window"].sudo().browse(action_id)
                modes = [v.strip() for v in (act_rec.view_mode or "").split(",") if v.strip()]
                if modes:
                    default_view = "list" if modes[0] in ("list", "tree") else modes[0]

                # Certains menus (ex: "Devis") n'ont pas de domain direct sur
                # l'action — le filtrage vient d'un filtre de recherche par défaut
                # (context 'search_default_xxx'). _resolve_action_domain() (déjà
                # utilisé par database_controller.py pour list_records) combine
                # les deux sources correctement, plutôt que de relire seulement
                # act_rec.domain qui serait vide/trompeur dans ce cas.
                resolved = self._resolve_action_domain(env, model_name, action_id)
                if resolved:
                    menu_domain = resolved

            menus.append({
                "id": menu.id,
                "name": menu.name,
                "parent_id": menu.parent_id.id if menu.parent_id else None,
                "sequence": menu.sequence,
                "model": model_name,
                "action_id": action_id,
                "default_view": default_view,
                "domain": menu_domain,
            })

        # Regroupe, pour chaque modèle, les actions (menus) qui l'utilisent
        # réellement — une action peut imposer sa propre vue tree/form via
        # view_ids, différente de la vue "par défaut" du modèle (ex: Devis
        # vs Commandes dans Ventes utilisent deux vues tree différentes).
        model_actions = {}
        for m in menus:
            if m["model"] and m["action_id"]:
                model_actions.setdefault(m["model"], set()).add(m["action_id"])

        views_payload = {}
        fields_payload = {}

        for model_name in models_used:
            if model_name not in env:
                continue

            Model = env[model_name]
            views_payload[model_name] = {"default": {}, "by_action": {}}

            # Vue par défaut du modèle (comportement historique, sert de repli
            # pour les types de vue qu'une action ne précise pas).
            for view_type in ("form", "list", "tree", "kanban", "pivot", "graph"):
                try:
                    view_data = Model.get_view(view_type=view_type)
                except Exception as e:
                    _logger.info("Vue %s non disponible pour %s: %s", view_type, model_name, e)
                    continue

                key = "list" if view_type in ("list", "tree") else view_type
                if key in views_payload[model_name]["default"]:
                    continue

                views_payload[model_name]["default"][key] = {
                    "arch": view_data.get("arch"),
                    "view_id": view_data.get("id"),
                }

            # Vues spécifiques à chaque action pointant vers ce modèle.
            for action_id in model_actions.get(model_name, []):
                try:
                    action = env["ir.actions.act_window"].sudo().browse(action_id)
                    if not action.exists():
                        continue
                except Exception as e:
                    _logger.info("Action %s introuvable: %s", action_id, e)
                    continue

                # 1. Surcharges explicites de vue par type, définies sur l'action
                #    (table ir.actions.act_window.view — c'est là que Devis pointe
                #    vers view_quotation_tree, différent de la vue tree par défaut).
                explicit_view_ids = {}
                for view_link in action.view_ids:
                    vtype = view_link.view_mode
                    key = "list" if vtype in ("list", "tree") else vtype
                    if key not in ("form", "list", "kanban", "pivot", "graph"):
                        continue
                    if key not in explicit_view_ids and view_link.view_id:
                        explicit_view_ids[key] = view_link.view_id.id

                # 2. La vue "principale" de l'action (champ view_id singulier),
                #    si son type correspond à un des types qu'on gère.
                if action.view_id:
                    main_type = "list" if action.view_id.type == "tree" else action.view_id.type
                    if main_type in ("form", "list", "kanban", "pivot", "graph"):
                        explicit_view_ids.setdefault(main_type, action.view_id.id)

                # 3. Tous les types déclarés par l'action (view_mode), même sans
                #    surcharge explicite — on laisse alors Odoo choisir sa vue
                #    par défaut pour ce type (view_id=None).
                declared_modes = [v.strip() for v in (action.view_mode or "").split(",") if v.strip()]
                for vtype in declared_modes:
                    key = "list" if vtype in ("list", "tree") else vtype
                    if key not in ("form", "list", "kanban", "pivot", "graph"):
                        continue
                    explicit_view_ids.setdefault(key, None)

                action_payload = {}
                for key, view_id in explicit_view_ids.items():
                    try:
                        view_data = Model.get_view(
                            view_id=view_id,
                            view_type="tree" if key == "list" else key,
                        )
                    except Exception as e:
                        _logger.info(
                            "Vue %s (view_id=%s) non disponible pour %s via action %s: %s",
                            key, view_id, model_name, action_id, e
                        )
                        continue
                    action_payload[key] = {
                        "arch": view_data.get("arch"),
                        "view_id": view_data.get("id"),
                    }

                # Complète avec les vues par défaut pour les types non couverts.
                for key, view_info in views_payload[model_name]["default"].items():
                    action_payload.setdefault(key, view_info)

                if action_payload:
                    views_payload[model_name]["by_action"][str(action_id)] = action_payload

            all_fields_info = Model.fields_get()
            fields_payload[model_name] = {}
            for fname, finfo in all_fields_info.items():
                if fname in self.IGNORED_FIELDS:
                    continue
                if finfo.get("type") not in self.SUPPORTED_TYPES:
                    continue

                field_data = {
                    "label": finfo.get("string", fname),
                    "type": finfo.get("type"),
                    "required": finfo.get("required", False),
                    "selection": finfo.get("selection") if finfo.get("type") == "selection" else None,
                    "relation": finfo.get("relation") if finfo.get("type") in ("many2one", "one2many", "many2many") else None,
                }

                if finfo.get("type") == "one2many":
                    field_data["relation_field"] = finfo.get("relation_field")
                    related_model = finfo.get("relation")
                    if related_model and related_model in env:
                        related_fields_info = env[related_model].fields_get()
                        sub_fields = {}
                        allowed_sub_types = self.SUPPORTED_TYPES - {"one2many"}
                        for sub_fname, sub_finfo in related_fields_info.items():
                            if sub_fname in self.IGNORED_FIELDS:
                                continue
                            if sub_finfo.get("type") not in allowed_sub_types:
                                continue
                            sub_fields[sub_fname] = {
                                "label": sub_finfo.get("string", sub_fname),
                                "type": sub_finfo.get("type"),
                                "required": sub_finfo.get("required", False),
                                "selection": sub_finfo.get("selection") if sub_finfo.get("type") == "selection" else None,
                                "relation": sub_finfo.get("relation") if sub_finfo.get("type") in ("many2one", "many2many") else None,
                            }
                        field_data["sub_fields"] = sub_fields

                fields_payload[model_name][fname] = field_data


        return self._cors_response(json.dumps({
            "module": {
                "technical_name": module_rec.name,
                "label": module_rec.shortdesc,
            },
            "models": list(models_used),
            "fields": fields_payload,
            "views": views_payload,
            "menus": menus,
        }))
