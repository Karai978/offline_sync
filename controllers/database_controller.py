from odoo import http #type: ignore
from odoo.http import request #type: ignore
from odoo.exceptions import AccessError #type: ignore
import json

from .common import OfflineSyncMixin


class DatabaseController(http.Controller, OfflineSyncMixin):
    """Accès générique aux données réelles : listes, fiches, listes de
    référence pour les dropdowns many2one. Rien ici n'est spécifique à un
    modèle en particulier (model vient toujours du paramètre de requête).

    Pour le dashboard Achats (Demandes de prix), spécifique à un seul
    modèle, voir purchase_dashboard_controller.py — séparé pour la même
    raison que catalog_controller.py : une feature Odoo native scopée à
    un modèle précis ne doit pas vivre dans un contrôleur générique."""

    @http.route("/offline_sync/reference_records", type="http", auth="none",
                methods=["GET", "OPTIONS"], csrf=False)
    def reference_records(self, model=None, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return self._cors_response()

        user = self._authenticate_api_key()
        if not user:
            return self._cors_response(json.dumps({"error": "Clé API invalide"}), status=401)

        if not model or model not in request.env:
            return self._cors_response(json.dumps({"error": "Modèle invalide"}), status=400)

        env = request.env(user=user.id)
        Model = env[model].sudo()

        records = Model.search([], limit=5000)

        if model == "res.currency":
            result = [
                {
                    "id": rec.id,
                    "display_name": rec.display_name,
                    "symbol": rec.symbol,
                    "position": rec.position,
                }
                for rec in records
            ]
        else:
            result = [
                {"id": rec.id, "display_name": rec.display_name}
                for rec in records
            ]

        return self._cors_response(json.dumps({"model": model, "records": result}))

    @http.route("/offline_sync/list_records", type="http", auth="none",
                methods=["GET", "OPTIONS"], csrf=False)
    def list_records(self, model=None, limit=80, offset=0, action=None, extra_domain=None, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return self._cors_response()

        user = self._authenticate_api_key()
        if not user:
            return self._cors_response(json.dumps({"error": "Clé API invalide"}), status=401)

        if not model or model not in request.env:
            return self._cors_response(json.dumps({"error": "Modèle invalide"}), status=400)

        env = request.env(user=user.id)
        Model = env[model]
        limit = int(limit)
        offset = int(offset)

        domain = self._resolve_action_domain(env, model, action)

        if extra_domain:
            try:
                parsed_extra = json.loads(extra_domain)
                if isinstance(parsed_extra, list):
                    domain += parsed_extra
            except (ValueError, TypeError):
                pass

        if "company_id" in Model._fields:
            domain += [
                "|",
                ("company_id", "=", False),
                ("company_id", "=", user.company_id.id),
            ]

        total = Model.search_count(domain)

        records = Model.search(domain, limit=limit, offset=offset, order="id desc")

        fields_info = Model.fields_get()
        field_names = [
            f for f, finfo in fields_info.items()
            if finfo.get("type") in self.SUPPORTED_TYPES and f not in self.IGNORED_FIELDS
        ]
        data = records.read(field_names) if records else []
        data = self._json_safe(data)

        return self._cors_response(json.dumps({
            "model": model,
            "total": total,
            "records": data,
        }))

    @http.route("/offline_sync/read_record", type="http", auth="none",
                methods=["GET", "OPTIONS"], csrf=False)
    def read_record(self, model=None, id=None, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return self._cors_response()

        user = self._authenticate_api_key()
        if not user:
            return self._cors_response(json.dumps({"error": "Clé API invalide"}), status=401)

        if not model or model not in request.env or not id:
            return self._cors_response(json.dumps({"error": "Paramètres invalides"}), status=400)

        env = request.env(user=user.id)
        Model = env[model]  # toujours sans .sudo()

        try:
            record = Model.browse(int(id))
            if not record.exists():
                return self._cors_response(json.dumps({"error": "Enregistrement introuvable"}), status=404)

            fields_info = Model.fields_get()
            field_names = [
                f for f, finfo in fields_info.items()
                if finfo.get("type") in self.SUPPORTED_TYPES and f not in self.IGNORED_FIELDS
            ]
            data = record.read(field_names)[0]
        except AccessError:
            return self._cors_response(
                json.dumps({"error": "Accès refusé à cet enregistrement"}), status=403
            )

        # Référence pour la détection de conflit à la synchro : capturée
        # à part (pas dans field_names, qui exclut write_date), sous une
        # clé technique distincte pour ne jamais être confondue avec un
        # champ métier normal.
        data["__reference_write_date__"] = record.write_date

        for fname, finfo in fields_info.items():
            if fname not in field_names:
                continue

            if finfo["type"] == "many2one" and data.get(fname):
                data[fname] = data[fname][0]

            elif finfo["type"] == "many2many" and data.get(fname):
                related_model = finfo.get("relation")
                if related_model and related_model in env:
                    ids = data[fname]
                    records = env[related_model].sudo().browse(ids)
                    data[fname] = [[r.id, r.display_name] for r in records if r.exists()]
                else:
                    data[fname] = []

            elif finfo["type"] == "one2many" and data.get(fname):
                line_ids = data[fname]
                sub_model = finfo["relation"]
                if sub_model not in env:
                    data[fname] = []
                    continue
                sub_fields_info = env[sub_model].fields_get()
                sub_field_names = [
                    f for f, fi in sub_fields_info.items()
                    if fi.get("type") in (self.SUPPORTED_TYPES - {"one2many"}) and f not in self.IGNORED_FIELDS
                ]
                lines = env[sub_model].sudo().browse(line_ids).read(sub_field_names)
                for line in lines:
                    for lf, lfi in sub_fields_info.items():
                        if lf in line and lfi.get("type") == "many2one" and line[lf]:
                            line[lf] = line[lf][0]
                        elif lfi.get("type") == "many2many" and line[lf]:
                            related_model = lfi.get("relation")
                            if related_model and related_model in env:
                                related_records = env[related_model].sudo().browse(line[lf])
                                line[lf] = [[r.id, r.display_name] for r in related_records if r.exists()]
                                data[fname] = lines

        data = self._json_safe(data)

        return self._cors_response(json.dumps({"record": data}))
