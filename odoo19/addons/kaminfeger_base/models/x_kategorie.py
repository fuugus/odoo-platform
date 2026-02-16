from odoo import models, fields


class XKategorie(models.Model):
    _name = "x_kategorie"
    _description = "Kategorie"
    x_active = fields.Boolean(string="Aktiv")
    x_avatar_image = fields.Binary(string="Kategorie Bild")
    x_name = fields.Char(string="Beschreibung", required=True, translate=True)
    x_studio_sequence = fields.Integer(string="Sequenz")
    x_studio_ubergeordnete_kategorie = fields.Many2one(string="Übergeordnete Kategorie", comodel_name="x_kategorie")
