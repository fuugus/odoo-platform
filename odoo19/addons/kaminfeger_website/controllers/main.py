from odoo import http
from odoo.http import request
from odoo.addons.website.controllers.main import Website
from odoo.addons.website_hr_recruitment.controllers.main import WebsiteHrRecruitment


class CustomJobController(WebsiteHrRecruitment):

    @http.route([
        '/jobs',
        '/jobs/country/<model("res.country"):country>',
        '/jobs/department/<model("hr.department"):department>',
        '/jobs/country/<model("res.country"):country>/department/<model("hr.department"):department>',
        '/jobs/office/<int:office_id>',
        '/jobs/country/<model("res.country"):country>/office/<int:office_id>',
        '/jobs/department/<model("hr.department"):department>/office/<int:office_id>',
        '/jobs/country/<model("res.country"):country>/department/<model("hr.department"):department>/office/<int:office_id>',
    ], type='http', auth="public", website=True, sitemap=True)
    def jobs(self, country=None, department=None, office_id=None, page=1, **kwargs):

        kanton_id = kwargs.get('kanton')

        domain = [('website_published', '=', True)]

        if kanton_id:
            domain.append(('address_id.state_id.id', '=', int(kanton_id)))
        if department:
            domain.append(('department_id', '=', department.id))
        if country:
            domain.append(('address_id.country_id', '=', country.id))
        if office_id:
            domain.append(('address_id', '=', office_id))

        Job = request.env['hr.job']

        job_count = Job.search_count(domain)

        pager_args = {}
        if kanton_id:
            pager_args['kanton'] = kanton_id

        pager = request.website.pager(
            url='/jobs',
            total=job_count,
            page=page,
            step=12,
            url_args=pager_args,
        )

        jobs = Job.search(domain,
                          order="is_published desc, no_of_recruitment desc",
                          limit=12,
                          offset=pager['offset'])

        jobs_for_canton_filter = Job.sudo().search([
            ('website_published', '=', True),
            ('address_id.state_id', '!=', False),
            ('address_id.country_id.code', '=', 'CH')
        ])

        active_canton_ids = jobs_for_canton_filter.mapped(
            'address_id.state_id.id')

        cantons = request.env['res.country.state'].search([
            ('id', 'in', active_canton_ids)
        ], order='name asc')

        values = {
            'jobs': jobs,
            'cantons': cantons,
            'current_kanton': int(kanton_id) if kanton_id else None,
            'department': department,
            'country': country,
            'office_id': office_id,
            'pager': pager,
        }

        return request.render("website_hr_recruitment.index", values)


class DownloadController(http.Controller):

    def _prepare_download_values(self, category=None):
        """
        Diese Funktion enthält nur die reine Logik zum Laden der Daten.
        Sie gibt ein Dictionary zurück, das wir später erweitern können.
        """

        file_domain = []
        if request.env.user._is_public():
            file_domain.append(('x_studio_nur_fur_mitglieder', '=', False))

        visible_files = request.env['x_datei'].sudo().search_read(
            file_domain, ['x_studio_kategorie']
        )
        cats_with_files_ids = set(f['x_studio_kategorie'][0]
                                  for f in visible_files if f['x_studio_kategorie'])

        all_cats_unchecked = request.env['x_kategorie'].sudo().search(
            [], order='x_studio_sequence asc')
        cat_map = {c.id: c for c in all_cats_unchecked}

        visible_cat_ids = set(cats_with_files_ids)
        stack = list(cats_with_files_ids)

        while stack:
            current_id = stack.pop()
            cat = cat_map.get(current_id)
            if cat and cat.x_studio_ubergeordnete_kategorie:
                parent_id = cat.x_studio_ubergeordnete_kategorie.id
                if parent_id not in visible_cat_ids:
                    visible_cat_ids.add(parent_id)
                    stack.append(parent_id)

        all_cats = all_cats_unchecked.filtered(
            lambda c: c.id in visible_cat_ids)

        root_categories = all_cats.filtered(
            lambda c: not c.x_studio_ubergeordnete_kategorie)
        current_category = None

        if category:
            target_cat = request.env['x_kategorie'].sudo().browse(
                int(category))
            if target_cat.id in visible_cat_ids:
                current_category = target_cat

        tiles_categories = request.env['x_kategorie']
        if current_category:
            tiles_categories = all_cats.filtered(
                lambda c: c.x_studio_ubergeordnete_kategorie.id == current_category.id)
        else:
            tiles_categories = root_categories

        files = []
        if current_category and current_category.exists():
            search_domain = file_domain + \
                [('x_studio_kategorie', '=', current_category.id)]
            files = request.env['x_datei'].sudo().search(
                search_domain, order='x_name asc')

        return {
            'root_categories': root_categories,
            'current_category': current_category,
            'tiles_categories': tiles_categories,
            'files': files,
            'get_children': lambda cat_id: all_cats.filtered(lambda c: c.x_studio_ubergeordnete_kategorie.id == cat_id),
        }

    @http.route(['/downloads'], type='http', auth='public', website=True, sitemap=True)
    def downloads(self, category=None, **kwargs):
        values = self._prepare_download_values(category)
        values.update({
            'page_key': 'downloads_main',
            'page_title': 'Downloads',
            'page_intro': 'Hier finden Sie wichtige Dokumente',
            'base_url': '/downloads',
        })
        return request.render('kaminfeger_website.downloads_page', values)

    @http.route(['/mitgliederbereich'], type='http', auth='public', website=True, sitemap=True)
    def mitgliederbereich(self, category=None, **kwargs):
        values = self._prepare_download_values(category)
        values.update({
            'page_key': 'members_only',
            'page_title': 'Mitgliederbereich',
            'page_intro': 'Interne Dokumente',
            'base_url': '/mitgliederbereich',
        })
        return request.render('kaminfeger_website.downloads_page', values)


class KaminfegerverzeichnisController(http.Controller):

    @http.route(['/kaminfegerverzeichnis'], type='http', auth='public', website=True, sitemap=True)
    def kaminfeger_directory(self, kanton=None, **kwargs):

        active_companies = request.env['res.partner'].sudo().search([
            ('x_studio_ist_kaminfeger_unternehmen', '=', True),  # Nur Kaminfeger
            ('state_id', '!=', False)  # Nur mit gesetztem Kanton
        ])

        used_state_ids = active_companies.mapped('state_id.id')

        states = request.env['res.country.state'].sudo().search([
            # Nur Kantone, die wir oben gefunden haben
            ('id', 'in', used_state_ids),
            ('country_id.code', '=', 'CH')
        ], order='name asc')

        current_state = None
        if kanton:
            current_state = request.env['res.country.state'].sudo().browse(
                int(kanton))

        companies = []
        if current_state:
            companies = request.env['res.partner'].sudo().search([
                ('state_id', '=', current_state.id),
                ('x_studio_ist_kaminfeger_unternehmen', '=', True)
            ], order='name asc')

        return request.render('kaminfeger_website.kaminfeger_page', {
            'states': states,
            'current_state': current_state,
            'companies': companies,
        })


class PersonSearchController(http.Controller):

    @http.route('/feuko/search', type='http', auth='public', website=True)
    def search_person(self, **post):
        """
        Diese Methode wird aufgerufen, wenn der Button gedrückt wird.
        'post' enthält die Daten aus dem Formular: vorname, name, feuko_nr
        """

        vorname = post.get('vorname')
        nachname = post.get('name')
        feuko_nr = post.get('feuko_nr')

        domain = []

        if vorname:
            domain.append(('name', 'ilike', vorname))

        if nachname:
            domain.append(('name', 'ilike', nachname))

        if feuko_nr:
            domain.append(('x_studio_feuko_nummer', '=', feuko_nr))

        if not domain:
            partners = []
        else:
            domain.append(('is_company', '=', False))
            domain.append(('x_studio_feuko_nummer', '!=', False))

            partners = request.env['res.partner'].sudo().search(
                domain, limit=100)

        abschluesse = request.env['x_abschluss'].sudo().search(
            [], order='x_studio_sequence asc')

        return request.render('kaminfeger_website.feuko_page', {
            'partners': partners,
            'abschluesse': abschluesse,
            'search_params': post,
            'search_performed': bool(domain),
        })
