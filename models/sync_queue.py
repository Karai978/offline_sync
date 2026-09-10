from odoo import models, fields # type: ignore
from odoo.api import call_kw # type: ignore
from odoo.service.model import get_public_method # type: ignore
import json
import logging

from ..utils.conflict_detector import detect_conflicts
from ..utils.value_normalize import normalize_datetime, build_many2many_commands

_logger = logging.getLogger(__name__)


class SyncQueue(models.Model):
    """Core of the sync.queue model: fields, orchestration (apply_action), 
        and generic create/write/unlink/call_method execution."""

    _name = "sync.queue"
    _description = "File d'attente de synchronisation offline"
    _order = "created_at asc"

    local_uuid = fields.Char(string="UUID local", required=True, index=True)
    model_name = fields.Char(string="Modèle cible", required=True)
    operation = fields.Selection(
        [("create", "Create"), ("write", "Write"), ("unlink", "Unlink"),
         ("call_method", "Appel de méthode")],
        string="Opération", required=True,
    )
    payload = fields.Text(string="Payload (JSON)", required=True)
    status = fields.Selection(
        [("pending", "En attente"), ("in_progress", "En cours"),
         ("sent", "Synchronisé"), ("error", "Erreur"), ("conflict", "Conflit en attente")],
        string="Statut", default="pending", required=True, index=True,
    )
    created_at = fields.Datetime(string="Créé le (client)", required=True)
    synced_at = fields.Datetime(string="Synchronisé le")
    error_message = fields.Text(string="Message d'erreur")
    odoo_record_id = fields.Integer(string="ID enregistrement Odoo créé")

    # Snapshot captured by the PWA when the form loads, to
    # detect whether the record has been modified elsewhere in the meantime.
    reference_write_date = fields.Datetime(string="write_date de référence")
    reference_values = fields.Text(string="Valeurs de référence (JSON)")
    has_conflict = fields.Boolean(string="Contient un conflit", default=False)
    conflict_details = fields.Text(string="Détails du conflit (JSON)")

    # Populated only for operation="call_method" when the called method
    # returned an action (dict) rather than None/True—this applies to
    # "composite" buttons that delegate to a wizard (e.g., action_cancel on
    # sale.order opens sale.order.cancel instead of cancelling directly).
    # Distinct from error_message: this is NOT a failure; the status remains 'sent',
    # the server call succeeded, but its full effect requires follow-up
    # that automatic synchronization cannot complete on its own.
    requires_manual_action = fields.Boolean(string="Nécessite une action manuelle", default=False)
    pending_action = fields.Text(string="Action serveur en attente (JSON)")

    _sql_constraints = [
        ("local_uuid_unique", "unique(local_uuid)",
         "Cette action a déjà été reçue (UUID déjà existant)."),
    ]

    # =========================================================================
    # ORCHESTRATION
    # =========================================================================

    def apply_action(self):
        """Exécute l'opération (create/write/unlink/call_method) sur model_name,
        écrit le statut final, et ne laisse jamais une exception remonter telle
        quelle (toujours transformée en statut 'error' exploitable).

        Générique par construction : model_name et operation viennent
        entièrement du payload envoyé par la PWA, aucune branche de code
        ici n'est spécifique à un modèle Odoo en particulier."""
        self.ensure_one()
        try:
            data = json.loads(self.payload)
            with self.env.cr.savepoint():
                record_id, conflicts, requires_manual_action, pending_action_json = (
                    self._execute_normalized(data)
                )

            has_conflict = bool(conflicts)
            final_status = "conflict" if has_conflict else "sent"
            conflict_details_json = json.dumps(conflicts) if has_conflict else False

            self.write({
                "status": final_status,
                "has_conflict": has_conflict,
                "conflict_details": conflict_details_json,
                "synced_at": fields.Datetime.now(),
                "error_message": False,
                "odoo_record_id": record_id or 0,
                "requires_manual_action": requires_manual_action,
                "pending_action": pending_action_json or False,
            })
            return {
                "local_uuid": self.local_uuid,
                "status": final_status,
                "odoo_record_id": self.odoo_record_id,
                "conflicts": conflicts if has_conflict else None,
                "requires_manual_action": requires_manual_action,
                "pending_action": json.loads(pending_action_json) if pending_action_json else None,
            }

        except Exception as e:
            _logger.exception("Erreur de synchronisation pour %s", self.local_uuid)
            self.write({
                "status": "error",
                "error_message": str(e),
                "requires_manual_action": False,
                "pending_action": False,
            })
            return {"local_uuid": self.local_uuid, "status": "error", "error": str(e)}

    def resolve_conflict(self, resolution):
        """Arbitre un conflit détecté par apply_action() : 'local' force
        l'écriture avec les valeurs que la PWA avait tenté d'écrire (ignore
        le conflit), 'server' abandonne l'action et garde la valeur serveur
        telle quelle. Ne revérifie PAS si un nouveau changement a eu lieu
        entre la détection du conflit et cette résolution (fenêtre de
        vérification volontairement simplifiée — voir discussion projet :
        un triple conflit dans cette fenêtre est jugé suffisamment rare
        pour être une limite connue plutôt qu'un cas géré)."""
        self.ensure_one()

        if resolution == "server":
            # Abandonne l'action locale : rien à écrire, on marque juste
            # l'entrée comme résolue sans modification en base.
            self.write({
                "status": "sent",
                "has_conflict": False,
                "conflict_details": False,
                "synced_at": fields.Datetime.now(),
                "error_message": False,
            })
            return {
                "local_uuid": self.local_uuid,
                "status": "sent",
                "resolution": "server",
                "odoo_record_id": self.odoo_record_id,
            }

        # resolution == "local" : force l'écriture, en ignorant le conflit
        try:
            data = json.loads(self.payload)
            data.pop("id", None)  # l'id est géré séparément, pas un champ à écrire

            target_model = self.env[self.model_name]
            record = target_model.browse(self.odoo_record_id)
            if not record.exists():
                raise ValueError(f"Record {self.model_name}#{self.odoo_record_id} not found")

            with self.env.cr.savepoint():
                prepared = self._prepare_values(target_model, data, is_create=False)
                if prepared:
                    record.write(prepared)

            self.write({
                "status": "sent",
                "has_conflict": False,
                "conflict_details": False,
                "synced_at": fields.Datetime.now(),
                "error_message": False,
            })
            return {
                "local_uuid": self.local_uuid,
                "status": "sent",
                "resolution": "local",
                "odoo_record_id": self.odoo_record_id,
            }

        except Exception as e:
            _logger.exception("Erreur lors de la résolution du conflit pour %s", self.local_uuid)
            self.write({"status": "error", "error_message": str(e)})
            return {"local_uuid": self.local_uuid, "status": "error", "error": str(e)}

    def _execute_normalized(self, data):
        """Wraps _execute() to always return a 4-element tuple
        (record_id, conflict_detected, requires_manual_action, pending_action_json),
        regardless of the operation — create/write/unlink always return
        requires_manual_action=False and pending_action_json=None; only
        call_method can populate the latter two."""
        result = self._execute(data)
        if len(result) == 4:
            return result
        record_id, conflicts = result
        return record_id, conflicts, False, None

    # =========================================================================
    # EXECUTION (generic create/write/unlink/call_method, called from apply_action)
    # =========================================================================

    def _execute(self, data):
        """Executes the create/write/unlink/call_method operation on model_name,
        converting one2many/many2many fields (simple lists sent by the PWA)
        into the command format expected by the Odoo ORM.
        For write operations.

        model_name and operation originate entirely from the queue (and
        thus from the PWA payload)—this method has no prior knowledge of
        specific Odoo model names; it works identically for sale.order,
        res.partner, or any other model."""
        target_model = self.env[self.model_name]

        if self.operation == "create":
            prepared = self._prepare_values(target_model, data, is_create=True)
            record = target_model.create(prepared)
            return record.id, False

        elif self.operation == "write":
            data = dict(data)
            record_id = data.pop("id")
            record = target_model.browse(record_id)
            if not record.exists():
                raise ValueError(
                    f"Record {self.model_name}#{record_id} not found "
                )

            reference = json.loads(self.reference_values) if self.reference_values else None
            fields_info = target_model.fields_get()
            conflicts = detect_conflicts(record, data, reference, fields_info)
            if conflicts:
                return record_id, conflicts

            prepared = self._prepare_values(target_model, data, is_create=False)
            if prepared:
                record.write(prepared)
            return record_id, []

        elif self.operation == "unlink":
            record_id = data.get("id")
            target_model.browse(record_id).unlink()
            return record_id, False

        elif self.operation == "call_method":
            return self._execute_call_method(target_model, data)

    def _execute_call_method(self, target_model, data):
        """Executes a public method triggered by an Odoo view button
        with `type="object"` (e.g., `action_confirm`, `action_cancel`).

        Reuses the native Odoo mechanism exactly (`odoo.api.call_kw` +
        `odoo.service.model.get_public_method`)—the same one used
        by the native `/web/dataset/call_button` endpoint—thereby
        ensuring the same security guarantees (rejection of private,
        `@api.private`, or technical methods) without the need to
        maintain a custom whitelist.

        Generic: `method_name`, `args`, and `kwargs` are derived entirely
        from the PWA payload. No method or model names are hardcoded
        here; this branch works for any `type="object"` button on
        any Odoo model."""
        data = dict(data)
        raw_id = data.pop("id")
        method_name = data.pop("method")
        args = data.pop("args", [])
        kwargs = data.pop("kwargs", {})

        ids = raw_id if isinstance(raw_id, list) else [raw_id]

        record = target_model.browse(ids)
        if not record.exists():
            raise ValueError(
                f"Record(s) {self.model_name}#{ids} not found"
            )

        # Native Odoo security: raises AttributeError if the method does not exist,
        # AccessError if it is private/technical — propagates as-is
        # to apply_action(), which converts this into a usable 'error' status.
        get_public_method(target_model, method_name)

        result = call_kw(target_model, method_name, [ids] + list(args), kwargs)

        requires_manual_action = False
        pending_action_json = None
        if isinstance(result, dict) and result.get("type") != "":
            requires_manual_action = True
            pending_action_json = json.dumps(self._json_safe_result(result))

        return ids[0], False, requires_manual_action, pending_action_json

    def _json_safe_result(self, value):
        """Recursively convert an Odoo action result (which may contain dates or non-serializable objects)
        into a JSON-safe structure for storage in pending_action."""
        import datetime as dt

        if isinstance(value, (dt.date, dt.datetime)):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: self._json_safe_result(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._json_safe_result(v) for v in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    # =========================================================================
    # PREPARING VALUES (simple JSON sent by the PWA -> Odoo ORM format)
    # =========================================================================

    def _prepare_values(self, target_model, data, is_create=True):
        """Convertit les valeurs brutes envoyées par la PWA vers le format
        attendu par l'ORM Odoo."""
        prepared = {}
        fields_info = target_model.fields_get()

        WRITABLE_FALSY_TYPES = ("boolean", "integer", "float", "monetary")

        for fname, value in data.items():
            finfo = fields_info.get(fname)
            if not finfo:
                continue

            if value is None:
                continue

            if (value is False or value == "") and finfo["type"] not in WRITABLE_FALSY_TYPES:
                continue

            if finfo["type"] == "one2many" and isinstance(value, list):
                prepared[fname] = self._build_one2many_commands(value, finfo["relation"])
            elif finfo["type"] == "many2many" and isinstance(value, list):
                prepared[fname] = build_many2many_commands(value)
            elif finfo["type"] == "datetime" and value:
                prepared[fname] = normalize_datetime(value, self.env.user.tz)
            else:
                prepared[fname] = value

        return prepared

    def _build_one2many_commands(self, lines, comodel_name):
        """Converts a list of simple dicts sent by the PWA into
        Odoo ORM commands:
        - line with 'id' and '_deleted' = True -> (2, id, 0) = deletion
        - line with 'id' -> (1, id, {...}) = update of an existing line
        - line without 'id' -> (0, 0, {...}) = creation of a new line

        comodel_name: the actual model of the lines (e.g., 'sale.order.line',
        'purchase.order.line', or any other one2many) — passed
        by the caller rather than inferred, so that _clean_line() reads the
        fields of the CORRECT model."""
        commands = []
        for line in lines:
            line_id = line.get("id")

            if line_id and line.get("_deleted"):
                commands.append((2, line_id, 0))
                continue

            cleaned = self._clean_line(line, comodel_name)
            cleaned.pop("id", None)
            cleaned.pop("_deleted", None)

            if line_id:
                commands.append((1, line_id, cleaned))
            else:
                commands.append((0, 0, cleaned))
        return commands

    def _clean_line(self, line, comodel_name):
        """Populates a one2many line with missing product information
        (name, unit of measure) — requires self.env, so it remains a method
        rather than a pure function in utils/.

        Generic: reads fields from the actual sub-model (comodel_name),
        regardless of whether it is sale.order.line, purchase.order.line,
        or any other model with product-based lines."""
        cleaned = {k: v for k, v in line.items() if v is not False}

        template_id = cleaned.pop("product_template_id", None)
        if template_id and not cleaned.get("product_id"):
            template = self.env["product.template"].sudo().browse(template_id)
            if template.exists():
                variant = template.product_variant_id
                if variant:
                    cleaned["product_id"] = variant.id

        if cleaned.get("product_id"):
            product = self.env["product.product"].sudo().browse(cleaned["product_id"])
            if product.exists():
                if not cleaned.get("name"):
                    cleaned["name"] = product.display_name

                line_fields = self.env[comodel_name]._fields
                uom_field_name = None
                for candidate in ("product_uom", "product_uom_id"):
                    if candidate in line_fields:
                        uom_field_name = candidate
                        break

                if uom_field_name and not cleaned.get(uom_field_name):
                    cleaned[uom_field_name] = product.uom_id.id

        return cleaned