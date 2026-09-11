{
    "name": "Offline Sync",
    "version": "17.0.1.0.0",
    "summary": "Synchronisation des données saisies hors ligne (PWA) vers Odoo",
    "category": "Technical",
    "author": "VLR.34",
    "depends": ["base", "web"],
    "data": [
        "security/ir.model.access.csv",
        "views/res_users_views.xml",
    ],
    "installable": True,
    "application": False,
}