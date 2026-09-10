"""Fonctions utilitaires de sérialisation JSON, partagées par les modèles
et les contrôleurs du module.

Aucune dépendance à l'ORM Odoo — pures fonctions Python, utilisables
depuis n'importe quel modèle ou contrôleur du module sans rien dupliquer.
"""
import datetime as dt
import json


def json_safe(value):
    """Convertit récursivement les objets date/datetime Python en strings,
    au format natif Odoo (ex: '2026-08-24 14:30:00'), pour toute structure
    de données (dict, list, ou valeur scalaire) avant sérialisation JSON.

    Utilisée quand la valeur convertie peut être réinjectée telle quelle
    dans un champ Date/Datetime Odoo plus tard (ex: reference_write_date)."""
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def safe_json_dumps(value):
    """Sérialise une valeur en JSON en gérant les types non nativement
    sérialisables (date, datetime) via un fallback isoformat() — évite
    l'erreur 'Object of type datetime is not JSON serializable'.

    Utilisée pour stocker une valeur "figée" en base (reference_value,
    local_value, server_value, resolved_value sur sync.conflict) — le
    format isoformat suffit, la valeur n'a pas besoin d'être réinjectée
    directement dans un champ Odoo depuis cette sérialisation."""
    def default(o):
        if isinstance(o, (dt.datetime, dt.date)):
            return o.isoformat()
        return str(o)

    return json.dumps(value, default=default)
