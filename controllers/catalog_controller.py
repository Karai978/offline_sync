from odoo import http  # type: ignore
from odoo.http import request  # type: ignore
import logging
import json

from .common import OfflineSyncMixin

_logger = logging.getLogger(__name__)


class CatalogController(http.Controller, OfflineSyncMixin):

    CATALOG_DOMAIN_FIELD = {
        "sale.order": "sale_ok",
        "purchase.order": "purchase_ok",
    }

    @http.route(
            "/offline_sync/catalog/products",
            type="http",
            auth="none",
            methods=["GET", "OPTIONS"],
            csrf=False
        )
    def catalog_products(self, model=None, partner_id=None, **kwargs):
        if request.httprequest.method == "OPTIONS":
            return self._cors_response()

        user = self._authenticate_api_key()
        if not user:
            return self._cors_response(json.dumps({"error": "Invalid API key"}), status=401)

        if not model or model not in self.CATALOG_DOMAIN_FIELD:
            return self._cors_response(
                json.dumps({"error": "Invalid or missing model parameter"}),
                status=400,
            )

        env = request.env(user=user.id)
        Product = env["product.product"]

        domain_field = self.CATALOG_DOMAIN_FIELD[model]
        domain = [(domain_field, "=", True)]

        partner = None
        if partner_id:
            try:
                partner_id_int = int(partner_id)
            except (TypeError, ValueError):
                return self._cors_response(json.dumps({"error": "Invalid partner_id"}), status=400)
            partner = env["res.partner"].browse(partner_id_int)
            if not partner.exists():
                partner = None

        # Purchases: show only products for which this supplier
        # is actually listed (product.supplierinfo / seller_ids) —
        # replicates the behavior of the "Supplier" filter.
        if model == "purchase.order" and partner:
            domain.append(("seller_ids.partner_id", "=", partner.id))

        try:
            products = Product.search(domain, limit=500)
        except Exception as e:
            _logger.exception("Error searching catalog products")
            return self._cors_response(json.dumps({"error": str(e)}), status=500)

        result = []
        for product in products:
            price = product.list_price
            min_qty = None
            supplier_warning = None

            if model == "purchase.order":
                price = product.standard_price
                if partner:
                    try:
                        seller = product._select_seller(
                            partner_id=partner,
                            quantity=None,
                            uom_id=product.uom_id,
                            ordered_by="min_qty",
                        )
                    except Exception as e:
                        _logger.info("Supplier selection not possible for product %s: %s", product.id, e)
                        seller = None

                    if seller:
                        price = seller.price
                        min_qty = seller.min_qty

            image_base64 = product.image_128.decode("utf-8") if product.image_128 else None

            result.append({
                "id": product.id,
                "product_tmpl_id": product.product_tmpl_id.id,
                "name": product.display_name,
                "default_code": product.default_code or None,
                "price": price,
                "min_qty": min_qty,
                "qty_available": product.qty_available,
                "virtual_available": product.virtual_available,
                "uom": {
                    "id": product.uom_id.id,
                    "name": product.uom_id.name,
                },
                "categ_id": product.categ_id.id if product.categ_id else None,
                "categ_name": product.categ_id.name if product.categ_id else None,
                "image_base64": f"data:image/png;base64,{image_base64}" if image_base64 else None,
            })

        return self._cors_response(json.dumps(self._json_safe({
            "model": model,
            "partner_id": partner.id if partner else None,
            "count": len(result),
            "products": result,
        })))