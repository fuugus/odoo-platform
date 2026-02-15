{
    'name': 'Kaminfeger Website',
    'description': 'Website theme and pages for Kaminfeger Schweiz',
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
        'kaminfeger_base',
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
            'kaminfeger_website/static/src/scss/primary_variables.scss',
        ],
        'web.assets_frontend': [
            'kaminfeger_website/static/src/scss/theme.scss',
            'kaminfeger_website/static/src/js/theme.js',

            'kaminfeger_website/static/src/scss/snippets/kfintro.scss',
            'kaminfeger_website/static/src/scss/snippets/kfsimpleintro.scss',
            'kaminfeger_website/static/src/scss/snippets/kfdoubleblock.scss',
            'kaminfeger_website/static/src/scss/snippets/kfbenefits.scss',
            'kaminfeger_website/static/src/scss/snippets/kfnews.scss',
            'kaminfeger_website/static/src/scss/snippets/kfteam.scss',
            'kaminfeger_website/static/src/scss/snippets/kfkontakt.scss',
            'kaminfeger_website/static/src/scss/snippets/accordion.scss',
        ],
        'website.assets_editor': [
            'kaminfeger_website/static/src/website_builder/footer_option.xml',
        ],
    },
    'license': 'LGPL-3',
    'installable': True,
}
