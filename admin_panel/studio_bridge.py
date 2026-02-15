"""
Studio Bridge — Export Studio customizations to proper Odoo modules and revert.

Functions:
- get_db_connection(db_name) — PostgreSQL connection via local socket
- get_custom_addons_info(config) — Scan repo addon directories
- export_studio_to_module(db_name, client, version, ws_send) — Studio → module
- convert_module_to_studio(db_name, client, ws_send) — Module → Studio (pre-upgrade)
- install_or_upgrade_module(instance_name, module_name, operation, ws_send) — odoo-bin -i/-u
"""
import ast
import os
import re
import textwrap
from pathlib import Path

import psycopg2
import psycopg2.extras

from config import load_config, PLATFORM_DIR
from setup_steps import run_cmd, odoo_base_dir


FIELD_TYPE_MAP = {
    "char": "Char",
    "text": "Text",
    "html": "Html",
    "integer": "Integer",
    "float": "Float",
    "boolean": "Boolean",
    "date": "Date",
    "datetime": "Datetime",
    "binary": "Binary",
    "selection": "Selection",
    "many2one": "Many2one",
    "one2many": "One2many",
    "many2many": "Many2many",
    "monetary": "Monetary",
}

STANDARD_MODEL_MODULES = {
    "res.partner": "base",
    "res.company": "base",
    "res.users": "base",
    "res.country.state": "base",
    "product.template": "product",
    "product.product": "product",
    "sale.order": "sale",
    "sale.order.line": "sale",
    "purchase.order": "purchase",
    "account.move": "account",
    "account.move.line": "account",
    "hr.employee": "hr",
    "hr.job": "hr",
    "hr.department": "hr",
    "project.project": "project",
    "project.task": "project",
    "crm.lead": "crm",
    "stock.picking": "stock",
    "stock.move": "stock",
    "website.page": "website",
    "blog.post": "website_blog",
    "event.event": "event",
}


def get_db_connection(db_name):
    return psycopg2.connect(
        dbname=db_name,
        user="odoo",
        host="/var/run/postgresql",
    )


def get_custom_addons_info(config):
    result = {}
    for ver in ["19", "18"]:
        addons_dir = Path(PLATFORM_DIR) / f"odoo{ver}" / "addons"
        if not addons_dir.exists():
            continue
        addons = []
        for addon_path in sorted(addons_dir.iterdir()):
            if not addon_path.is_dir() or addon_path.name.startswith("."):
                continue
            manifest_path = addon_path / "__manifest__.py"
            if not manifest_path.exists():
                continue
            try:
                manifest = ast.literal_eval(manifest_path.read_text())
                addons.append({
                    "name": addon_path.name,
                    "display_name": manifest.get("name", addon_path.name),
                    "version": manifest.get("version", ""),
                    "summary": manifest.get("summary", manifest.get("description", "")[:120]),
                    "depends": manifest.get("depends", []),
                })
            except Exception:
                addons.append({
                    "name": addon_path.name,
                    "display_name": addon_path.name,
                    "version": "?",
                    "summary": "(manifest parse error)",
                    "depends": [],
                })
        if addons:
            result[ver] = addons
    return result


def _sanitize_model_filename(model_name):
    return model_name.replace(".", "_").replace("-", "_")


def _python_field_line(field, selections_map):
    ftype = field["ttype"]
    py_type = FIELD_TYPE_MAP.get(ftype)
    if not py_type:
        return None

    fname = field["name"]
    attrs = {}

    if field.get("field_description"):
        attrs["string"] = field["field_description"]

    if ftype == "selection":
        sel_key = (field["model"], fname)
        options = selections_map.get(sel_key, [])
        if options:
            sel_list = [(s["value"], s["name"]) for s in options]
            attrs["selection"] = sel_list

    if ftype == "many2one":
        if field.get("relation"):
            attrs["comodel_name"] = field["relation"]
        if field.get("on_delete") and field["on_delete"] != "set null":
            attrs["ondelete"] = field["on_delete"]

    if ftype == "one2many":
        if field.get("relation"):
            attrs["comodel_name"] = field["relation"]
        if field.get("relation_field"):
            attrs["inverse_name"] = field["relation_field"]

    if ftype == "many2many":
        if field.get("relation"):
            attrs["comodel_name"] = field["relation"]
        if field.get("relation_table"):
            attrs["relation"] = field["relation_table"]
        if field.get("column1"):
            attrs["column1"] = field["column1"]
        if field.get("column2"):
            attrs["column2"] = field["column2"]

    if field.get("required"):
        attrs["required"] = True
    if field.get("index"):
        attrs["index"] = True

    parts = []
    for k, v in attrs.items():
        if k == "selection":
            parts.append(f"selection={v!r}")
        elif isinstance(v, bool):
            parts.append(f"{k}={v}")
        elif isinstance(v, str):
            parts.append(f'{k}="{v}"')
        else:
            parts.append(f"{k}={v!r}")

    args = ", ".join(parts)
    return f"    {fname} = fields.{py_type}({args})"


async def export_studio_to_module(db_name, client, version, ws_send=None):
    ws = ws_send or (lambda m: None)

    module_name = f"{client}_base"
    addons_dir = Path(PLATFORM_DIR) / f"odoo{version}" / "addons"
    module_dir = addons_dir / module_name
    models_dir = module_dir / "models"
    security_dir = module_dir / "security"
    views_dir = module_dir / "views"

    await ws(f"Connecting to database {db_name}...")
    conn = get_db_connection(db_name)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        # 1. Custom models (state=manual means Studio-created)
        await ws("Querying custom models...")
        cur.execute("""
            SELECT id, model, name, info
            FROM ir_model
            WHERE state = 'manual'
            ORDER BY model
        """)
        custom_models = cur.fetchall()
        await ws(f"  Found {len(custom_models)} custom model(s)")

        # 2. Manual fields (Studio-created fields on any model)
        await ws("Querying Studio fields...")
        cur.execute("""
            SELECT f.id, f.name, f.field_description, f.ttype, f.state,
                   f.required, f.index, f.relation, f.relation_field,
                   f.relation_table, f.column1, f.column2, f.on_delete,
                   m.model, m.state as model_state
            FROM ir_model_fields f
            JOIN ir_model m ON f.model_id = m.id
            WHERE f.state = 'manual'
            ORDER BY m.model, f.name
        """)
        all_fields = cur.fetchall()
        await ws(f"  Found {len(all_fields)} Studio field(s)")

        # 3. Selection options
        await ws("Querying selection options...")
        cur.execute("""
            SELECT f.model, f.name as field_name, s.value, s.name, s.sequence
            FROM ir_model_fields_selection s
            JOIN ir_model_fields f ON s.field_id = f.id
            WHERE f.state = 'manual'
            ORDER BY f.model, f.name, s.sequence
        """)
        selections_raw = cur.fetchall()
        selections_map = {}
        for s in selections_raw:
            key = (s["model"], s["field_name"])
            selections_map.setdefault(key, []).append(s)
        await ws(f"  Found {len(selections_raw)} selection option(s)")

        # 4. Access rules for custom models
        await ws("Querying access rules...")
        cur.execute("""
            SELECT a.name, m.model,
                   COALESCE(d.module || '.' || d.name, '') as group_xmlid,
                   a.perm_read, a.perm_write, a.perm_create, a.perm_unlink
            FROM ir_model_access a
            JOIN ir_model m ON a.model_id = m.id
            LEFT JOIN ir_model_data d ON d.model = 'res.groups' AND d.res_id = a.group_id
            WHERE m.state = 'manual'
            ORDER BY m.model, a.name
        """)
        access_rules = cur.fetchall()
        await ws(f"  Found {len(access_rules)} access rule(s)")

        # 5. Studio views
        await ws("Querying Studio views...")
        cur.execute("""
            SELECT v.id, v.name, v.model, v.type, v.arch_db, v.inherit_id,
                   v.priority, v.active,
                   d.name as xmlid_name, d.module as xmlid_module
            FROM ir_ui_view v
            JOIN ir_model_data d ON d.model = 'ir.ui.view' AND d.res_id = v.id
            WHERE d.module = 'studio_customization'
            ORDER BY v.model, v.name
        """)
        studio_views = cur.fetchall()
        await ws(f"  Found {len(studio_views)} Studio view(s)")

        # Group fields by model
        fields_by_model = {}
        for f in all_fields:
            fields_by_model.setdefault(f["model"], []).append(f)

        custom_model_names = {m["model"] for m in custom_models}
        inherited_models = {
            model for model in fields_by_model
            if model not in custom_model_names
        }

        # Determine dependencies
        depends = set()
        for model in inherited_models:
            dep = STANDARD_MODEL_MODULES.get(model)
            if dep:
                depends.add(dep)
        depends.discard("base")
        depends = sorted(depends)
        if not depends:
            depends = ["base"]

        # Generate module files
        await ws(f"\nGenerating module {module_name}...")

        for d in [module_dir, models_dir, security_dir, views_dir]:
            d.mkdir(parents=True, exist_ok=True)

        model_files = []
        all_models = set()

        # Custom models (with _name)
        for model in custom_models:
            model_name = model["model"]
            all_models.add(model_name)
            filename = _sanitize_model_filename(model_name)
            model_files.append(filename)
            fields = fields_by_model.get(model_name, [])

            lines = [
                "from odoo import models, fields",
                "",
                "",
                f"class {_class_name(model_name)}(models.Model):",
                f'    _name = "{model_name}"',
                f'    _description = "{model["name"]}"',
            ]
            for f in fields:
                fl = _python_field_line(f, selections_map)
                if fl:
                    lines.append(fl)

            if len(fields) == 0:
                lines.append("    pass")

            (models_dir / f"{filename}.py").write_text("\n".join(lines) + "\n")
            await ws(f"  models/{filename}.py ({len(fields)} fields)")

        # Inherited models (with _inherit)
        for model_name in sorted(inherited_models):
            all_models.add(model_name)
            filename = _sanitize_model_filename(model_name)
            model_files.append(filename)
            fields = fields_by_model[model_name]

            lines = [
                "from odoo import models, fields",
                "",
                "",
                f"class {_class_name(model_name)}(models.Model):",
                f'    _inherit = "{model_name}"',
            ]
            for f in fields:
                fl = _python_field_line(f, selections_map)
                if fl:
                    lines.append(fl)

            (models_dir / f"{filename}.py").write_text("\n".join(lines) + "\n")
            await ws(f"  models/{filename}.py (inherit, {len(fields)} fields)")

        # models/__init__.py
        init_lines = [f"from . import {f}" for f in sorted(set(model_files))]
        (models_dir / "__init__.py").write_text("\n".join(init_lines) + "\n")

        # security/ir.model.access.csv
        if access_rules:
            csv_lines = ["id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink"]
            for rule in access_rules:
                model_under = rule["model"].replace(".", "_")
                rule_id = f"access_{model_under}_{rule['name'].replace(' ', '_').replace('.', '_').lower()}"
                model_ref = f"model_{model_under}"
                group_ref = rule["group_xmlid"] or ""
                r = int(rule["perm_read"])
                w = int(rule["perm_write"])
                c = int(rule["perm_create"])
                u = int(rule["perm_unlink"])
                csv_lines.append(f"{rule_id},{rule['name']},{model_ref},{group_ref},{r},{w},{c},{u}")
            (security_dir / "ir.model.access.csv").write_text("\n".join(csv_lines) + "\n")
            await ws(f"  security/ir.model.access.csv ({len(access_rules)} rules)")

        # views/studio_views.xml
        if studio_views:
            xml_lines = ['<?xml version="1.0" encoding="utf-8"?>', "<odoo>"]
            for view in studio_views:
                arch = view["arch_db"] or ""
                # Extract the inner content if wrapped in data tag
                xml_lines.append(f"")
                xml_lines.append(f"    <record id=\"{view['xmlid_name']}\" model=\"ir.ui.view\">")
                xml_lines.append(f"        <field name=\"name\">{view['name']}</field>")
                xml_lines.append(f"        <field name=\"model\">{view['model']}</field>")
                xml_lines.append(f"        <field name=\"type\">{view['type']}</field>")
                if view.get("priority") and view["priority"] != 16:
                    xml_lines.append(f"        <field name=\"priority\">{view['priority']}</field>")
                if view.get("inherit_id"):
                    cur.execute("""
                        SELECT module || '.' || name FROM ir_model_data
                        WHERE model = 'ir.ui.view' AND res_id = %s LIMIT 1
                    """, (view["inherit_id"],))
                    parent_row = cur.fetchone()
                    if parent_row:
                        parent_xmlid = list(parent_row.values())[0]
                        xml_lines.append(f'        <field name="inherit_id" ref="{parent_xmlid}"/>')
                xml_lines.append(f"        <field name=\"arch\" type=\"xml\">")
                for arch_line in arch.strip().splitlines():
                    xml_lines.append(f"            {arch_line}")
                xml_lines.append(f"        </field>")
                xml_lines.append(f"    </record>")
            xml_lines.append("")
            xml_lines.append("</odoo>")
            (views_dir / "studio_views.xml").write_text("\n".join(xml_lines) + "\n")
            await ws(f"  views/studio_views.xml ({len(studio_views)} views)")

        # __init__.py
        (module_dir / "__init__.py").write_text("from . import models\n")

        # __manifest__.py
        data_files = []
        if access_rules:
            data_files.append("security/ir.model.access.csv")
        if studio_views:
            data_files.append("views/studio_views.xml")

        manifest = {
            "name": f"{client.title()} Base",
            "summary": f"Studio field definitions for {client}",
            "version": "1.0.0",
            "category": "Technical",
            "depends": depends,
            "data": data_files,
            "license": "LGPL-3",
            "installable": True,
        }
        (module_dir / "__manifest__.py").write_text(repr(manifest) + "\n")
        await ws(f"  __manifest__.py (depends: {depends})")

        await ws(f"\nModule generated at: {module_dir}")
        await ws(f"Models: {len(custom_models)} custom + {len(inherited_models)} inherited")
        await ws(f"Fields: {len(all_fields)} total")

    finally:
        cur.close()
        conn.close()


def _class_name(model_name):
    parts = model_name.replace(".", "_").replace("-", "_").split("_")
    return "".join(p.capitalize() for p in parts if p)


async def convert_module_to_studio(db_name, client, ws_send=None):
    ws = ws_send or (lambda m: None)
    module_name = f"{client}_base"

    await ws(f"Connecting to database {db_name}...")
    conn = get_db_connection(db_name)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        # Check module exists
        cur.execute(
            "SELECT state FROM ir_module_module WHERE name = %s",
            (module_name,)
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"Module {module_name} not found in database {db_name}")
        await ws(f"Module {module_name} state: {row['state']}")

        # Get all records owned by this module
        await ws("Finding records owned by module...")
        cur.execute(
            "SELECT id, model, res_id, name FROM ir_model_data WHERE module = %s ORDER BY model",
            (module_name,)
        )
        owned_records = cur.fetchall()
        await ws(f"  Found {len(owned_records)} owned record(s)")

        if not owned_records:
            await ws("No records to revert.")
            return

        field_records = [r for r in owned_records if r["model"] == "ir.model.fields"]
        model_records = [r for r in owned_records if r["model"] == "ir.model"]
        view_records = [r for r in owned_records if r["model"] == "ir.ui.view"]
        other_records = [r for r in owned_records if r["model"] not in ("ir.model.fields", "ir.model", "ir.ui.view")]

        # Revert fields to manual state
        if field_records:
            field_ids = [r["res_id"] for r in field_records]
            await ws(f"Reverting {len(field_ids)} field(s) to state=manual...")
            cur.execute(
                "UPDATE ir_model_fields SET state = 'manual' WHERE id = ANY(%s)",
                (field_ids,)
            )

        # Revert models to manual state
        if model_records:
            model_ids = [r["res_id"] for r in model_records]
            await ws(f"Reverting {len(model_ids)} model(s) to state=manual...")
            cur.execute(
                "UPDATE ir_model SET state = 'manual' WHERE id = ANY(%s)",
                (model_ids,)
            )

        # Transfer view ownership to studio_customization
        if view_records:
            view_data_ids = [r["id"] for r in view_records]
            await ws(f"Transferring {len(view_data_ids)} view(s) to studio_customization...")
            cur.execute(
                "UPDATE ir_model_data SET module = 'studio_customization' WHERE id = ANY(%s)",
                (view_data_ids,)
            )

        # Delete remaining ownership records (fields, models, access rules, etc.)
        non_view_ids = [r["id"] for r in owned_records if r["model"] != "ir.ui.view"]
        if non_view_ids:
            await ws(f"Removing {len(non_view_ids)} ownership record(s)...")
            cur.execute(
                "DELETE FROM ir_model_data WHERE id = ANY(%s)",
                (non_view_ids,)
            )

        # Mark module as uninstalled
        await ws(f"Setting {module_name} to uninstalled...")
        cur.execute(
            "UPDATE ir_module_module SET state = 'uninstalled' WHERE name = %s",
            (module_name,)
        )

        conn.commit()
        await ws(f"\nSuccessfully reverted {module_name} to Studio state.")
        await ws(f"  Fields reverted: {len(field_records)}")
        await ws(f"  Models reverted: {len(model_records)}")
        await ws(f"  Views transferred: {len(view_records)}")

    except Exception as e:
        conn.rollback()
        raise RuntimeError(f"Revert failed (rolled back): {e}")
    finally:
        cur.close()
        conn.close()


async def install_or_upgrade_module(instance_name, module_name, operation, ws_send=None):
    ws = ws_send or (lambda m: None)

    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise RuntimeError(f"Instance {instance_name} not found")

    version = instance.get("version", "19")
    base = odoo_base_dir(version)
    db_name = instance["db_name"]
    conf_path = instance["conf"]
    service = instance["service"]

    repo_addons = Path(PLATFORM_DIR) / f"odoo{version}" / "addons"
    instance_addons = f"{base}/data/{instance_name}/addons"

    flag = "-i" if operation == "install" else "-u"
    op_label = "Installing" if operation == "install" else "Upgrading"

    await ws(f"{op_label} {module_name} on {instance_name}...")

    # Stop instance
    await ws(f"Stopping {service}...")
    await run_cmd(f"systemctl stop {service}", ws)

    # Rsync custom addons
    if repo_addons.exists():
        await ws("Syncing custom addons from repo...")
        await run_cmd(
            f"rsync -a --exclude=.git --exclude=.gitkeep {repo_addons}/ {instance_addons}/",
            ws,
        )
        await run_cmd(f"chown -R odoo:odoo {instance_addons}", ws)

    # Run odoo-bin
    odoo_bin = f"{base}/odoo/odoo-bin"
    await ws(f"Running odoo-bin {flag} {module_name}...")
    await run_cmd(
        f"su - odoo -s /bin/bash -c '{base}/venv/bin/python {odoo_bin} "
        f"-c {conf_path} {flag} {module_name} --stop-after-init "
        f"--no-http 2>&1'",
        ws,
    )

    # Start instance
    await ws(f"Starting {service}...")
    await run_cmd(f"systemctl start {service}", ws)

    await ws(f"\n{module_name} {operation}ed successfully on {instance_name}.")
