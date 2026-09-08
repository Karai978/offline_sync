"""Détection de conflits de synchronisation offline-first..."""

import datetime as dt

COMPARABLE_TYPES = {
    "char", "text", "integer", "float", "boolean",
    "selection", "date", "datetime", "monetary", "many2one",
}


def detect_conflicts(record, data, reference_values_json, fields_info=None):
    if not reference_values_json:
        return []

    fields_info = fields_info or {}
    conflicts = []

    for field_name, local_value in data.items():
        if field_name not in reference_values_json:
            continue

        finfo = fields_info.get(field_name)
        field_type = finfo.get("type") if finfo else None

        if field_type == "one2many":
            comodel_name = finfo.get("relation") if finfo else None
            reference_lines = reference_values_json.get(field_name)
            if not comodel_name or not isinstance(local_value, list) or not isinstance(reference_lines, list):
                continue
            conflicts.extend(
                _detect_line_conflicts(record, field_name, local_value, reference_lines, comodel_name)
            )
            continue

        if field_type == "many2many":
            reference_ids = reference_values_json.get(field_name)
            conflict = _detect_many2many_conflict(record, field_name, local_value, reference_ids)
            if conflict:
                conflicts.append(conflict)
            continue

        reference_value = reference_values_json.get(field_name)
        local_norm = _normalize_value_for_type(local_value, field_type)
        reference_norm = _normalize_value_for_type(reference_value, field_type)

        if local_norm == reference_norm:
            continue

        server_value = _normalize_for_compare(record[field_name])
        server_norm = _normalize_value_for_type(server_value, field_type)

        if server_norm != reference_norm:
            conflicts.append({
                "field": field_name,
                "local_value": local_value,
                "server_value": server_value,
                "server_write_date": _normalize_for_compare(record.write_date),
            })

    return conflicts


def _detect_many2many_conflict(record, field_name, local_ids, reference_ids):
    """Compare un champ many2many comme un ensemble d'ids (l'ordre n'a
    pas d'importance). Même principe à deux conditions que les champs
    scalaires : conflit seulement si Luck ET le serveur ont TOUS DEUX
    changé la liste par rapport à la référence commune. Pas de merge
    automatique des ajouts/retraits — résolution binaire comme le reste
    du système (garder local / garder serveur en bloc)."""
    if not isinstance(local_ids, list) or not isinstance(reference_ids, list):
        return None

    local_set = set(local_ids)
    reference_set = set(reference_ids)

    if local_set == reference_set:
        return None  # Luck n'a rien changé sur ce champ

    server_ids = record[field_name].ids
    server_set = set(server_ids)

    if server_set == reference_set:
        return None  # le serveur n'a rien changé, pas de conflit

    if local_set == server_set:
        return None  # même résultat final des deux côtés, rien à arbitrer

    return {
        "field": field_name,
        "local_value": sorted(local_set),
        "server_value": sorted(server_set),
        "server_write_date": _normalize_for_compare(record.write_date),
    }


def _detect_line_conflicts(record, field_name, local_lines, reference_lines, comodel_name):
    """Compare les lignes one2many une par une, appariées par id.
    Nouvelle ligne (pas d'id) : jamais de conflit, ignorée."""
    conflicts = []
    reference_by_id = {l.get("id"): l for l in reference_lines if l.get("id")}
    server_by_id = {line.id: line for line in record[field_name]}
    server_write_date = _normalize_for_compare(record.write_date)

    for local_line in local_lines:
        line_id = local_line.get("id")
        if not line_id:
            continue

        reference_line = reference_by_id.get(line_id)
        if reference_line is None:
            continue

        server_line = server_by_id.get(line_id)
        is_deleted_locally = bool(local_line.get("_deleted"))

        if server_line is None:
            # Le serveur a supprimé cette ligne entre-temps.
            if is_deleted_locally:
                continue  # supprimée des deux côtés : pas de conflit
            changed = any(
                k not in ("id", "_deleted") and local_line.get(k) != reference_line.get(k)
                for k in local_line
            )
            if changed:
                conflicts.append({
                    "field": f"{field_name}[{line_id}]",
                    "local_value": local_line,
                    "server_value": None,
                    "server_write_date": server_write_date,
                    "conflict_type": "edit_vs_delete",
                })
            continue

        if is_deleted_locally:
            # Il a supprimé la ligne ; le serveur l'a-t-il modifiée ?
            changed_fields = {}
            for sub_field, ref_val in reference_line.items():
                if sub_field == "id" or sub_field not in server_line._fields:
                    continue
                server_val = _normalize_for_compare(server_line[sub_field])
                if server_val != ref_val:
                    changed_fields[sub_field] = server_val
            if changed_fields:
                conflicts.append({
                    "field": f"{field_name}[{line_id}]",
                    "local_value": "SUPPRIMÉ",
                    "server_value": changed_fields,
                    "server_write_date": server_write_date,
                    "conflict_type": "delete_vs_edit",
                })
            continue

        # Ligne présente et non supprimée des deux côtés : sous-champ par sous-champ.
        for sub_field, local_val in local_line.items():
            if sub_field in ("id", "_deleted"):
                continue
            if sub_field not in reference_line or sub_field not in server_line._fields:
                continue

            reference_val = reference_line.get(sub_field)
            if local_val == reference_val:
                continue  # non modifié par Luck

            server_val = _normalize_for_compare(server_line[sub_field])
            if server_val != reference_val:
                conflicts.append({
                    "field": f"{field_name}[{line_id}].{sub_field}",
                    "local_value": local_val,
                    "server_value": server_val,
                    "server_write_date": server_write_date,
                })

    return conflicts


def _normalize_value_for_type(value, field_type):
    if field_type == "datetime" and value:
        try:
            return str(value).replace("T", " ")[:19]
        except Exception:
            return value
    if field_type == "date" and value:
        return str(value)[:10]
    return value


def _normalize_for_compare(value):
    import odoo.models as odoo_models #type: ignore

    if isinstance(value, odoo_models.BaseModel):
        return value.id if value else False
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    return value