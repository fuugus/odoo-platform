{
    'name': 'KF Theme',
    'description': 'Theme for Kaminfeger Schweiz',
    'version': '1.0.0',
    'author': 'BINARY one GmbH',
    'category': 'Website/Theme',
    'depends': [
        'web',
        'website',
        'website_sale',
        'website_blog',
        'website_event',
        'hr',
        'website_hr_recruitment',
    ],
    'data': [
        'data/website.xml',
        'data/menu.xml',

        'views/website_assets.xml',

        'views/website_templates.xml',
        'views/blog_template.xml',
        'views/head_templates.xml',
        'views/header_templates.xml',
        'views/snippets.xml',
        'views/notfound_templates.xml',
        'views/login_templates.xml',

        'views/jobs_page.xml',
        'views/downloads_page.xml',
        'views/kaminfeger_page.xml',
        'views/feuko_page.xml',

        'views/snippets/options.xml',
        'views/snippets/s_snippet_kfintro.xml',
        'views/snippets/s_snippet_kfsimpleintro.xml',
        'views/snippets/s_snippet_kfdoubleblock1.xml',
        'views/snippets/s_snippet_kfdoubleblock2.xml',
        'views/snippets/s_snippet_kfdoubleblock3.xml',
        'views/snippets/s_snippet_kfdoubleblockaccordion.xml',
        'views/snippets/s_snippet_kfbenefits.xml',
        'views/snippets/s_snippet_kfnews.xml',
        'views/snippets/s_snippet_kfteam.xml',
        'views/snippets/s_snippet_kfkontakt.xml',
        'views/snippets/s_snippet_kffeuko.xml',
    ],
    'demo': [
        'demo/demo_data.xml',  # Add your demo file here
    ],
    'assets': {
        'web._assets_primary_variables': [
            'website_kftheme/static/src/scss/primary_variables.scss',
        ],
        'web.assets_frontend': [
            'website_kftheme/static/src/scss/theme.scss',
            'website_kftheme/static/src/js/theme.js',

            'website_kftheme/static/src/scss/snippets/kfintro.scss',
            'website_kftheme/static/src/scss/snippets/kfsimpleintro.scss',
            'website_kftheme/static/src/scss/snippets/kfdoubleblock.scss',
            'website_kftheme/static/src/scss/snippets/kfbenefits.scss',
            'website_kftheme/static/src/scss/snippets/kfnews.scss',
            'website_kftheme/static/src/scss/snippets/kfteam.scss',
            'website_kftheme/static/src/scss/snippets/kfkontakt.scss',
            'website_kftheme/static/src/scss/snippets/accordion.scss',
        ],
        'website.assets_editor': [
            'website_kftheme/static/src/website_builder/footer_option.xml',
        ],
    },
    'license': 'LGPL-3',
    'installable': True,
}
