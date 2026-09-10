from odoo import http #type: ignore
from odoo.http import request #type: ignore
import json

from .common import OfflineSyncMixin


class PurchaseDashboardController(http.Controller, OfflineSyncMixin):
    """Dashboard Achats (Demandes de prix) — réplique le dashboard natif
    Odoo pour ce menu précis. Volontairement scopé à purchase.order/
    purchase.order.line : comme le vrai Odoo, ce dashboard n'existe que
    pour ce menu, ce n'est pas une donnée générique.

    Séparé de database_controller.py (générique) pour la même raison que
    catalog_controller.py : une feature métier scopée ne doit pas gonfler
    un contrôleur censé fonctionner pour n'importe quel modèle."""

    @http.route("/offline_sync/purchase_dashboard", type="http", auth="none",
                methods=["GET", "OPTIONS"], csrf=False)
    def purchase_dashboard(self, action=None, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return self._cors_response()

        user = self._authenticate_api_key()
        if not user:
            return self._cors_response(json.dumps({"error": "Clé API invalide"}), status=401)

        env = request.env(user=user.id)

        # Restriction au menu "Demandes de prix" précisément (pas Commandes,
        # qui partage le même modèle purchase.order mais un dashboard différent).
        rfq_action_id = env["ir.model.data"]._xmlid_to_res_id(
            "purchase.purchase_rfq", raise_if_not_found=False
        )
        if not action or not rfq_action_id or int(action) != rfq_action_id:
            return self._cors_response(json.dumps({"applicable": False}))

        from odoo import fields as odoo_fields  # type: ignore
        from datetime import timedelta

        PurchaseOrder = env["purchase.order"]
        base_domain = self._resolve_action_domain(env, "purchase.order", action)
        today = odoo_fields.Date.context_today(env.user)

        domain_a_envoyer = base_domain + [("state", "=", "draft")]
        domain_en_attente = base_domain + [("state", "=", "sent")]
        domain_en_retard = base_domain + [
            ("state", "in", ("draft", "sent")),
            ("date_order", "<", today),
        ]

        def count(domain, mine):
            d = domain + [("user_id", "=", user.id)] if mine else domain
            return PurchaseOrder.search_count(d)

        result = {
            "applicable": True,
            "toutes": {
                "a_envoyer": count(domain_a_envoyer, False),
                "en_attente": count(domain_en_attente, False),
                "en_retard": count(domain_en_retard, False),
            },
            "mes": {
                "a_envoyer": count(domain_a_envoyer, True),
                "en_attente": count(domain_en_attente, True),
                "en_retard": count(domain_en_retard, True),
            },
        }

        week_ago = today - timedelta(days=7)

        confirmed_domain = [("state", "in", ("purchase", "done"))]
        confirmed_orders = PurchaseOrder.search(confirmed_domain)
        avg_order_value = (
            sum(confirmed_orders.mapped("amount_total")) / len(confirmed_orders)
            if confirmed_orders else 0.0
        )

        purchased_7d_orders = PurchaseOrder.search(
            confirmed_domain + [("date_approve", ">=", str(week_ago))]
        )
        purchased_7d = sum(purchased_7d_orders.mapped("amount_total"))

        sent_7d_count = PurchaseOrder.search_count(
            [("state", "=", "sent"), ("date_order", ">=", str(week_ago))]
        )

        PurchaseOrderLine = env["purchase.order.line"]
        lines = PurchaseOrderLine.search([
            ("order_id.state", "in", ("purchase", "done")),
            ("date_planned", "!=", False),
            ("order_id.date_order", "!=", False),
        ], limit=1000)
        delays = []
        for line in lines:
            if line.date_planned and line.order_id.date_order:
                delta = (line.date_planned.date() - line.order_id.date_order.date()).days
                if delta >= 0:
                    delays.append(delta)
        lead_time_days = round(sum(delays) / len(delays)) if delays else 0

        currency = env.company.currency_id
        result["kpi"] = {
            "avg_order_value": round(avg_order_value, 2),
            "purchased_7d": round(purchased_7d, 2),
            "lead_time_days": lead_time_days,
            "sent_7d_count": sent_7d_count,
            "currency_symbol": currency.symbol,
            "currency_position": currency.position,
        }

        return self._cors_response(json.dumps(result))
