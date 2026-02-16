from odoo import models, fields


class XAbschluss(models.Model):
    _name = "x_abschluss"
    _description = "Abschluss"
    x_active = fields.Boolean(string="Aktiv")
    x_name = fields.Char(string="Abschluss", required=True, translate=True)
    x_studio_sequence = fields.Integer(string="Sequenz")
