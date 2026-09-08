"""Détection générique du "modèle principal" d'une app Odoo installée —
utilisée à la fois par module_manifest() (pour savoir quelle branche de
menu garder) et par installed_apps() (pour l'afficher dans le dashboard
de la PWA).

Vivait auparavant comme méthode d'un seul contrôleur (metadata_controller)
alors que deux contrôleurs différents en ont besoin — extraite ici pour
être partagée sans dupliquer la logique.
"""

# Cache d'indices pour les apps déjà étudiées manuellement — évite de
# refaire la recherche par menu à chaque appel pour les cas déjà connus.
# Reste un simple raccourci : toute app absente de ce dictionnaire passe
# par l'heuristique générique ci-dessous (recherche par menu racine),
# donc rien ne casse si une app manque ici.
KNOWN_MAIN_MODELS = {
    "sale_management": "sale.order",
    "purchase": "purchase.order",
    "account": "account.move",
    "crm": "crm.lead",
}


def guess_main_model(env, module_name, is_technical_model):
    """Devine le modèle principal d'une app : d'abord dans le cache
    KNOWN_MAIN_MODELS, sinon en repli générique via l'action du premier
    menu racine (ou un de ses enfants) qui pointe vers un vrai modèle
    métier (non technique).

    is_technical_model : callable(model_name) -> bool, fourni par
    l'appelant (OfflineSyncMixin._is_technical_model) — évite de dupliquer
    cette heuristique ici, ce module n'a pas besoin de la connaître."""
    if module_name in KNOWN_MAIN_MODELS:
        return KNOWN_MAIN_MODELS[module_name]

    IrModelData = env["ir.model.data"].sudo()
    Module = env["ir.module.module"].sudo()

    module_rec = Module.search([("name", "=", module_name)], limit=1)
    candidate_modules = [module_name]
    if module_rec:
        candidate_modules += module_rec.dependencies_id.mapped("name")

    for candidate in candidate_modules:
        top_menu_data = IrModelData.search([
            ("module", "=", candidate),
            ("model", "=", "ir.ui.menu"),
        ])

        root_menus = []
        for data in top_menu_data:
            menu = env["ir.ui.menu"].sudo().browse(data.res_id)
            if not menu.exists() or menu.parent_id:
                continue
            root_menus.append(menu)

        for root_menu in root_menus:
            if root_menu.action and root_menu.action.type == "ir.actions.act_window":
                action = env["ir.actions.act_window"].sudo().browse(root_menu.action.id)
                if action.res_model and not is_technical_model(action.res_model):
                    return action.res_model

            children = env["ir.ui.menu"].sudo().search(
                [("parent_id", "child_of", root_menu.id)],
                order="sequence, id",
            )
            for child in children:
                if child.action and child.action.type == "ir.actions.act_window":
                    action = env["ir.actions.act_window"].sudo().browse(child.action.id)
                    if action.res_model and not is_technical_model(action.res_model):
                        return action.res_model

    return None
