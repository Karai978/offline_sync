"""Fonctions pures de normalisation de valeurs — aucune n'a besoin de
self.env, elles ne dépendent que de leurs arguments. Extraites de
sync_queue.py où elles vivaient comme méthodes sans raison de l'être.

Réutilisables par n'importe quel modèle du module, pour n'importe quel
champ Odoo — rien ici n'est spécifique à un modèle en particulier.
"""


def normalize_datetime(value):
    """Convertit le format HTML natif (YYYY-MM-DDTHH:MM) envoyé par la PWA
    vers le format attendu par Odoo (YYYY-MM-DD HH:MM:SS)."""
    if not value:
        return value
    value = value.replace("T", " ")
    if len(value) == 16:  # "YYYY-MM-DD HH:MM" sans les secondes
        value += ":00"
    return value


def normalize_raw(value):
    """Normalise une valeur brute issue de record.read() ou envoyée par
    la PWA, pour rendre comparables les différents formats équivalents :
    many2one -> (id, nom) devient id ; many2many -> liste de (id, nom)
    ou liste d'ids devient une liste d'ids triée ; le reste est laissé
    tel quel."""
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and isinstance(value[0], int) and isinstance(value[1], str):
            return value[0]
        ids = []
        for v in value:
            if isinstance(v, (list, tuple)) and v:
                ids.append(v[0])
            elif isinstance(v, int):
                ids.append(v)
        return sorted(ids) if ids else list(value)
    return value


def build_many2many_commands(ids):
    """Transforme une liste d'IDs simple (envoyée par la PWA) en commande
    ORM Odoo : (6, 0, [ids]) = remplace tout le contenu du many2many
    par cette liste. C'est la commande la plus simple et la plus fiable
    pour une synchronisation complète depuis un client offline."""
    clean_ids = [int(i) for i in ids if i]
    return [(6, 0, clean_ids)]
