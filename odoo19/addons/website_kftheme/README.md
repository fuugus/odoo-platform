# Kaminfeger Odoo Theme

## Setup

Download and install Odoo: https://www.odoo.com/documentation/19.0/administration/on_premise/source.html

Make sure to also download odoo enterprise.

Start Odoo with the parent folder of this repository included in the `--addons-path` parameter.

`python3 odoo-bin --addons-path={enterprise-path},{folder of this repo} -d {database-name} --dev=xml`

### Setup with db dump

Get db from here: https://drive.google.com/drive/folders/17bfnuC7Bcxw0GkiPLYm2-DvsuRf0LS0-?usp=drive_link

Go to http://localhost:8069/web/database/manager and press restore.

When asked for master password use: https://start.1password.com/open/i?a=SLFEMRFDNRBOJBUG5QFY747IIY&v=wnzusilxg35zawj2n2tsfb6olu&i=5tnsgw7cm6gwzofmayzsdyagjq&h=binaryone.1password.com

### Setup without db dump

Go to apps activate the website app. Make sure website_sales, website_event and website_blog are installed too.

Then search for "kftheme" and install this addon.

Go to the website editor and make sure the header template "Menu - Sales 3" is active and the correct theme color preset is chosen (at the bottom of color presets red, black, white).

Now you should be able to see the changes from this addon on the website.
