from odoo import models, fields


class ResCountryState(models.Model):
    _inherit = "res.country.state"
    x_studio_webseite_beschreibung = fields.Html(string="Webseite Beschreibung", translate=True)
