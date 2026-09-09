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
    if not isinstance(local_ids, list) or not isinstance(reference_ids, list):
        return None

    def extract_id(v):
        if isinstance(v, (list, tuple)) and len(v) >= 1:
            return v[0]
        return v

    local_set = set(extract_id(v) for v in local_ids)
    reference_set = set(extract_id(v) for v in reference_ids)

    if local_set == reference_set:
        return None

    server_ids = record[field_name].ids
    server_set = set(server_ids)

    if server_set == reference_set:
        return None

    if local_set == server_set:
        return None

    return {
        "field": field_name,
        "local_value": sorted(local_set),
        "server_value": sorted(server_set),
        "server_write_date": _normalize_for_compare(record.write_date),
    }


def _detect_line_conflicts(record, field_name, local_lines, reference_lines, comodel_name):
    """Compare les lignes one2many une par une, appariées par id."""
    conflicts = []
    reference_by_id = {l.get("id"): l for l in reference_lines if l.get("id")}
    server_by_id = {line.id: line for line in record[field_name]}
    server_write_date = _normalize_for_compare(record.write_date)

    line_fields_info = {}
    if comodel_name in record.env:
        line_fields_info = record.env[comodel_name].fields_get()

    def is_comparable(field_type):
        # one2many niché : hors scope (mêmes limites que le champ
        # racine order_line lui-même — pas de comparaison à deux niveaux
        # d'imbrication). many2many/many2one/scalaires : comparables.
        return field_type != "one2many"

    def normalize_ref_or_local(value, field_type):
        if field_type == "many2many":
            if not isinstance(value, list):
                return []
            return sorted(_extract_id(v) for v in value)
        if field_type == "datetime" and value:
            return str(value).replace("T", " ")[:19]
        if field_type == "date" and value:
            return str(value)[:10]
        return value

    def normalize_server(server_line, sub_field, field_type):
        if field_type == "many2many":
            return sorted(server_line[sub_field].ids)
        if field_type == "many2one":
            val = server_line[sub_field]
            return val.id if val else False
        return _normalize_for_compare(server_line[sub_field])

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
            if is_deleted_locally:
                continue
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
            changed_fields = {}
            for sub_field, ref_val in reference_line.items():
                if sub_field == "id" or sub_field not in server_line._fields:
                    continue
                field_type = line_fields_info.get(sub_field, {}).get("type")
                if not is_comparable(field_type):
                    continue

                ref_norm = normalize_ref_or_local(ref_val, field_type)
                server_norm = normalize_server(server_line, sub_field, field_type)
                if server_norm != ref_norm:
                    changed_fields[sub_field] = server_norm

            if changed_fields:
                conflicts.append({
                    "field": f"{field_name}[{line_id}]",
                    "local_value": "SUPPRIMÉ",
                    "server_value": changed_fields,
                    "server_write_date": server_write_date,
                    "conflict_type": "delete_vs_edit",
                })
            continue

        for sub_field, local_val in local_line.items():
            if sub_field in ("id", "_deleted"):
                continue
            if sub_field not in reference_line or sub_field not in server_line._fields:
                continue

            field_type = line_fields_info.get(sub_field, {}).get("type")
            if not is_comparable(field_type):
                continue

            reference_val = reference_line.get(sub_field)
            local_norm = normalize_ref_or_local(local_val, field_type)
            reference_norm = normalize_ref_or_local(reference_val, field_type)

            if local_norm == reference_norm:
                continue

            server_norm = normalize_server(server_line, sub_field, field_type)
            if server_norm != reference_norm:
                conflicts.append({
                    "field": f"{field_name}[{line_id}].{sub_field}",
                    "local_value": local_val,
                    "server_value": server_norm,
                    "server_write_date": server_write_date,
                })

    return conflicts


def _extract_id(v):
    if isinstance(v, (list, tuple)) and len(v) >= 1:
        return v[0]
    return v


def _extract_id(v):
    """Normalise un élément de liste many2many : accepte soit un id brut,
    soit une paire [id, label] (format read_record() pour les many2many),
    et retourne toujours l'id seul."""
    if isinstance(v, (list, tuple)) and len(v) >= 1:
        return v[0]
    return v


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