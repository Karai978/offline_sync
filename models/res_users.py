from odoo import models, fields # type: ignore
import secrets


class ResUsers(models.Model):
    _inherit = "res.users"

    offline_sync_api_key = fields.Char(
        string="Your API key",
        copy=False,
        groups="base.group_user",
    )

    def action_generate_offline_sync_key(self):
        """Génère une nouvelle clé API pour la synchronisation PWA."""
        for user in self:
            user.offline_sync_api_key = secrets.token_urlsafe(32)