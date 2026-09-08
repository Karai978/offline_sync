from odoo import http #type: ignore
from odoo.http import request #type: ignore
import json

from .common import OfflineSyncMixin, find_tmp_refs, substitute_tmp_refs


class SyncController(http.Controller, OfflineSyncMixin):

    @http.route(
        "/offline_sync/push",
        type="http",
        auth="none",
        methods=["POST", "OPTIONS"], 
        csrf=False)
    def push_actions(self, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return self._cors_response()

        user = self._authenticate_api_key()
        if not user:
            return self._cors_response(
                json.dumps({"error": "Clé API invalide ou manquante"}), status=401
            )

        try:
            body = json.loads(request.httprequest.data)
            actions = body.get("actions", [])
        except Exception:
            actions = []

        env = request.env(user=user.id)
        SyncQueue = env["sync.queue"]
        results = []

        # Crée les entrées de file (ou récupère celles déjà connues, pour
        # ne jamais rejouer deux fois la même action côté serveur).
        pending_entries = []
        for action in actions:
            local_uuid = action.get("local_uuid")
            existing = SyncQueue.search([("local_uuid", "=", local_uuid)], limit=1)
            if existing:
                results.append({
                    "local_uuid": local_uuid,
                    "status": existing.status,
                    "odoo_record_id": existing.odoo_record_id,
                })
                continue

            try:
                decoded_payload = json.loads(action.get("payload") or "{}")
            except Exception:
                decoded_payload = {}

            queue_entry = SyncQueue.create({
                "local_uuid": local_uuid,
                "model_name": action.get("model_name"),
                "operation": action.get("operation"),
                "payload": action.get("payload"),
                "created_at": action.get("created_at"),
                "reference_write_date": action.get("reference_write_date"),
                "reference_values": action.get("reference_values"),
                "status": "pending",
            })
            pending_entries.append((queue_entry, decoded_payload))

        # Table uuid local -> ID Odoo réel, pré-remplie avec les créations
        # déjà synchronisées lors d'un push précédent (retry).
        uuid_to_id = {
            entry.local_uuid: entry.odoo_record_id
            for entry in SyncQueue.search([("status", "=", "sent"), ("odoo_record_id", "!=", 0)])
        }

        # Traitement par passes successives : on exécute d'abord les
        # actions qui n'attendent aucune création "tmp:" non résolue,
        # ce qui fait progressivement apparaître les vrais IDs nécessaires
        # aux actions suivantes (ex: créer le produit avant la commande
        # qui le référence) — générique, quel que soit le modèle concerné.
        remaining = pending_entries
        max_passes = len(remaining) + 1
        for _ in range(max_passes):
            if not remaining:
                break
            still_waiting = []
            progressed = False

            for queue_entry, decoded_payload in remaining:
                unresolved = find_tmp_refs(decoded_payload) - set(uuid_to_id.keys())
                if unresolved:
                    still_waiting.append((queue_entry, decoded_payload))
                    continue

                resolved_payload = substitute_tmp_refs(decoded_payload, uuid_to_id)
                queue_entry.write({
                    "payload": json.dumps(resolved_payload),
                    "status": "in_progress",
                })
                result = queue_entry.apply_action()
                results.append(result)
                if result.get("status") == "sent":
                    uuid_to_id[queue_entry.local_uuid] = queue_entry.odoo_record_id
                progressed = True

            remaining = still_waiting
            if not progressed:
                break

        # Toute action encore bloquée après toutes les passes dépend d'une
        # création qui n'a jamais abouti (ex: le produit "tmp:" a lui-même
        # échoué) — on l'expose comme erreur explicite plutôt que de la
        # laisser silencieusement en "pending" pour toujours.
        for queue_entry, decoded_payload in remaining:
            queue_entry.write({
                "status": "error",
                "error_message": "Dépendance non résolue (un enregistrement créé en attente est introuvable).",
            })
            results.append({
                "local_uuid": queue_entry.local_uuid,
                "status": "error",
                "error": "Dépendance non résolue.",
            })

        return self._cors_response(json.dumps({"results": results}))

    @http.route("/offline_sync/resolve_conflict", type="http", auth="none",
        methods=["POST", "OPTIONS"], csrf=False)
    def resolve_conflict(self, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return self._cors_response()

        user = self._authenticate_api_key()
        if not user:
            return self._cors_response(
                json.dumps({"error": "Clé API invalide ou manquante"}), status=401
            )

        try:
            body = json.loads(request.httprequest.data)
            local_uuid = body.get("local_uuid")
            resolution = body.get("resolution")  # "local" ou "server"
        except Exception:
            return self._cors_response(
                json.dumps({"error": "Corps de requête invalide"}), status=400
            )

        if resolution not in ("local", "server"):
            return self._cors_response(
                json.dumps({"error": "resolution doit être 'local' ou 'server'"}), status=400
            )

        env = request.env(user=user.id)
        entry = env["sync.queue"].search([("local_uuid", "=", local_uuid)], limit=1)

        if not entry:
            return self._cors_response(
                json.dumps({"error": "Action introuvable"}), status=404
            )
        if entry.status != "conflict":
            return self._cors_response(
                json.dumps({"error": f"Cette action n'est pas en conflit (statut actuel : {entry.status})"}),
                status=409,
            )

        result = entry.resolve_conflict(resolution)
        return self._cors_response(json.dumps(result))