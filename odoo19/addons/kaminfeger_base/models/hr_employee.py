from odoo import models, fields


class HrEmployee(models.Model):
    _inherit = "hr.employee"
    x_studio_hover_bild = fields.Binary(string="Hover Bild")
