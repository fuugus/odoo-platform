from odoo import models, fields


class ResPartner(models.Model):
    _inherit = "res.partner"
    x_studio_abschlusse = fields.Many2many(string="Abschlüsse", comodel_name="x_abschluss", relation="x_res_partner_x_abschluss_rel", column1="res_partner_id", column2="x_abschluss_id")
    x_studio_date_field_1nv_1jf3ifk6b = fields.Date(string="Neu Datum")
    x_studio_feuko_nummer = fields.Char(string="Feuko-Nummer")
    x_studio_geburtsdatum = fields.Date(string="Geburtsdatum")
    x_studio_geburtsjahr = fields.Integer(string="Geburtsjahr")
    x_studio_heimatort = fields.Char(string="Heimatort")
    x_studio_ist_kaminfeger_unternehmen = fields.Boolean(string="Ist Kaminfeger Unternehmen")
    x_studio_ist_lehrbetrieb = fields.Boolean(string="Ist Lehrbetrieb")
    x_studio_ist_lehrling = fields.Boolean(string="Ist Lehrling")
    x_studio_lehrstart = fields.Date(string="Lehrstart")
    x_studio_mitgliedernummer = fields.Integer(string="Mitgliedernummer")
    x_studio_mitgliedstatus = fields.Selection(string="Mitgliedstatus", selection=[('Mitgliedstatus', 'Aktiv'), ('Nichtmitglied', 'Nichtmitglied'), ('Ehemalig', 'Ehemalig'), ('Partnermitglied', 'Partnermitglied')])
    x_studio_status_lehre = fields.Selection(string="Status Lehre", selection=[('Aktiv', 'Aktiv'), ('Abgeschlossen', 'Abgeschlossen'), ('Abgebrochen', 'Abgebrochen'), ('Unterbrochen', 'Unterbrochen'), ('Wiederholt 1. Lj', 'Wiederholt 1. Lj'), ('Wiederholt 2. Lj', 'Wiederholt 2. Lj'), ('Wiederholt 3. Lj', 'Wiederholt 3. Lj')])
