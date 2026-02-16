from odoo import models, fields


class XDatei(models.Model):
    _name = "x_datei"
    _description = "Datei"
    x_active = fields.Boolean(string="Aktiv")
    x_name = fields.Char(string="Dateiname", required=True, translate=True)
    x_studio_datei = fields.Binary(string="Datei")
    x_studio_datei_filename = fields.Char(string="Filename for x_studio_binary_field_1gv_1jf34itd8")
    x_studio_dateiname = fields.Char(string="Dateiname")
    x_studio_kategorie = fields.Many2one(string="Kategorie", comodel_name="x_kategorie")
    x_studio_nur_fur_mitglieder = fields.Boolean(string="Nur für Mitglieder")
    x_studio_sequence = fields.Integer(string="Sequenz")
