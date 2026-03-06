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
import shutil
import textwrap
from pathlib import Path

import psycopg2
import psycopg2.extras

from config import load_config, PLATFORM_DIR
from setup_steps import run_cmd, odoo_base_dir, get_instance_owner


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


def get_studio_stats(db_name, client, addon_names=None):
    """Return Studio customization stats for a database.

    Returns split counts (studio vs module) for fields, models, views,
    and optionally the install state of repo addons.
    """
    module_name = f"{client}_base"
    try:
        conn = get_db_connection(db_name)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Fields owned by the client module (authoritative source)
        cur.execute("""
            SELECT COUNT(*) as cnt FROM ir_model_data d
            JOIN ir_model_fields f ON d.res_id = f.id
            WHERE d.module = %s AND d.model = 'ir.model.fields'
              AND f.name LIKE 'x\\_%%'
              AND (f.related IS NULL OR f.related = '')
        """, (module_name,))
        module_fields = cur.fetchone()["cnt"]

        # Fields still in Studio (manual and NOT owned by the module)
        cur.execute("""
            SELECT COUNT(*) as cnt FROM ir_model_fields f
            WHERE f.state = 'manual'
              AND (f.related IS NULL OR f.related = '')
              AND NOT EXISTS (
                  SELECT 1 FROM ir_model_data d
                  WHERE d.module = %s AND d.model = 'ir.model.fields'
                    AND d.res_id = f.id
              )
        """, (module_name,))
        studio_fields = cur.fetchone()["cnt"]

        # Models owned by the client module
        cur.execute("""
            SELECT COUNT(*) as cnt FROM ir_model_data d
            JOIN ir_model m ON d.res_id = m.id
            WHERE d.module = %s AND d.model = 'ir.model'
              AND m.model LIKE 'x\\_%%'
        """, (module_name,))
        module_models = cur.fetchone()["cnt"]

        # Models still in Studio (manual and NOT owned by the module)
        cur.execute("""
            SELECT COUNT(*) as cnt FROM ir_model m
            WHERE m.state = 'manual'
              AND NOT EXISTS (
                  SELECT 1 FROM ir_model_data d
                  WHERE d.module = %s AND d.model = 'ir.model'
                    AND d.res_id = m.id
              )
        """, (module_name,))
        studio_models = cur.fetchone()["cnt"]

        # Views owned by the client module
        cur.execute("""
            SELECT COUNT(*) as cnt FROM ir_model_data
            WHERE module = %s AND model = 'ir.ui.view'
        """, (module_name,))
        module_views = cur.fetchone()["cnt"]

        # Views still in Studio (NOT owned by the module)
        cur.execute("""
            SELECT COUNT(*) as cnt FROM ir_model_data d
            WHERE d.module = 'studio_customization' AND d.model = 'ir.ui.view'
              AND NOT EXISTS (
                  SELECT 1 FROM ir_model_data m
                  WHERE m.module = %s AND m.model = 'ir.ui.view'
                    AND m.res_id = d.res_id
              )
        """, (module_name,))
        studio_views = cur.fetchone()["cnt"]

        # Custom fields defined in other custom modules (misplaced)
        cur.execute("""
            SELECT d.module, f.name, f.model
            FROM ir_model_data d
            JOIN ir_model_fields f ON d.res_id = f.id
            WHERE d.model = 'ir.model.fields'
              AND f.name LIKE 'x\\_%%'
              AND (f.related IS NULL OR f.related = '')
              AND d.module NOT IN (%s, 'studio_customization', 'base')
              AND d.module IN (
                  SELECT name FROM ir_module_module
                  WHERE author IS NULL OR author NOT LIKE '%%Odoo%%'
              )
        """, (module_name,))
        misplaced_fields = [dict(r) for r in cur.fetchall()]

        # Custom models defined in other custom modules (misplaced)
        cur.execute("""
            SELECT d.module, m.model
            FROM ir_model_data d
            JOIN ir_model m ON d.res_id = m.id
            WHERE d.model = 'ir.model'
              AND m.model LIKE 'x\\_%%'
              AND d.module NOT IN (%s, 'studio_customization', 'base')
              AND d.module IN (
                  SELECT name FROM ir_module_module
                  WHERE author IS NULL OR author NOT LIKE '%%Odoo%%'
              )
        """, (module_name,))
        misplaced_models = [dict(r) for r in cur.fetchall()]

        # Module state
        cur.execute("SELECT state FROM ir_module_module WHERE name = %s", (module_name,))
        row = cur.fetchone()
        module_state = row["state"] if row else "not_found"

        # Addon install states
        addons = {}
        if addon_names:
            cur.execute(
                "SELECT name, state FROM ir_module_module WHERE name = ANY(%s)",
                (list(addon_names),)
            )
            for r in cur.fetchall():
                addons[r["name"]] = r["state"]
            for n in addon_names:
                if n not in addons:
                    addons[n] = "not_found"

        cur.close()
        conn.close()

        total_fields = studio_fields + module_fields
        total_models = studio_models + module_models
        total_views = studio_views + module_views

        return {
            "fields": {"studio": studio_fields, "module": module_fields, "total": total_fields},
            "models": {"studio": studio_models, "module": module_models, "total": total_models},
            "views": {"studio": studio_views, "module": module_views, "total": total_views},
            "module_state": module_state,
            "module_name": module_name,
            "has_customizations": total_fields > 0 or total_models > 0 or total_views > 0,
            "misplaced_fields": misplaced_fields,
            "misplaced_models": misplaced_models,
            "addons": addons,
        }
    except Exception as e:
        return {"error": str(e)}


async def fix_misplaced_definitions(db_name, client, version, instance_name, ws_send=None):
    """Move misplaced field/model definitions back to studio_customization.

    For each misplaced field/model (owned by a custom module other than {client}_base):
    1. Transfer ir_model_data ownership to studio_customization
    2. Set state='manual' so Studio Bridge export picks them up
    3. Remove field definitions from the source module's Python files
    4. Git commit the file changes

    After running this, the user should run Studio → Module to include them in {client}_base.
    """
    async def ws(msg):
        if ws_send:
            await ws_send(msg)

    module_name = f"{client}_base"
    addons_dir = Path(odoo_base_dir(version)) / "data" / instance_name / "addons"

    stats = get_studio_stats(db_name, client)
    if "error" in stats:
        raise RuntimeError(f"Failed to get stats: {stats['error']}")

    misplaced_fields = stats.get("misplaced_fields", [])
    misplaced_models = stats.get("misplaced_models", [])

    if not misplaced_fields and not misplaced_models:
        await ws("No misplaced definitions found.")
        return

    await ws(f"Found {len(misplaced_fields)} misplaced field(s), {len(misplaced_models)} misplaced model(s)")

    conn = get_db_connection(db_name)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        # --- DB: Transfer ownership to studio_customization, set state=manual ---
        for f in misplaced_fields:
            await ws(f"  Transferring field {f['name']} on {f['model']} from {f['module']} to studio_customization...")
            cur.execute("""
                UPDATE ir_model_data
                SET module = 'studio_customization'
                WHERE module = %s AND model = 'ir.model.fields'
                  AND res_id = (
                      SELECT id FROM ir_model_fields
                      WHERE name = %s AND model = %s LIMIT 1
                  )
            """, (f["module"], f["name"], f["model"]))
            cur.execute("""
                UPDATE ir_model_fields SET state = 'manual'
                WHERE name = %s AND model = %s
            """, (f["name"], f["model"]))

        for m in misplaced_models:
            await ws(f"  Transferring model {m['model']} from {m['module']} to studio_customization...")
            cur.execute("""
                UPDATE ir_model_data
                SET module = 'studio_customization'
                WHERE module = %s AND model = 'ir.model'
                  AND res_id = (
                      SELECT id FROM ir_model m
                      WHERE m.model = %s LIMIT 1
                  )
            """, (m["module"], m["model"]))
            cur.execute("""
                UPDATE ir_model SET state = 'manual'
                WHERE model = %s
            """, (m["model"],))

        conn.commit()
        await ws("DB ownership transferred successfully.")

        # --- Files: Remove field definitions from source modules ---
        # Group misplaced items by (module, odoo_model) for efficient file editing
        affected_modules = set()
        fields_by_module_model = {}
        for f in misplaced_fields:
            key = (f["module"], f["model"])
            fields_by_module_model.setdefault(key, []).append(f["name"])
            affected_modules.add(f["module"])
        for m in misplaced_models:
            affected_modules.add(m["module"])

        for mod in sorted(affected_modules):
            mod_dir = addons_dir / mod
            if not mod_dir.exists():
                await ws(f"  Warning: module directory {mod} not found, skipping file cleanup")
                continue

            models_dir = mod_dir / "models"
            if not models_dir.exists():
                continue

            await ws(f"  Cleaning up files in {mod}/models/...")

            # Find all Python files that might contain field definitions
            for py_file in sorted(models_dir.glob("*.py")):
                if py_file.name == "__init__.py":
                    continue

                source = py_file.read_text()
                try:
                    tree = ast.parse(source)
                except SyntaxError:
                    await ws(f"    Warning: could not parse {py_file.name}, skipping")
                    continue

                # Collect field names to remove from this file
                fields_to_remove = set()
                for (fmod, fmodel), fnames in fields_by_module_model.items():
                    if fmod == mod:
                        fields_to_remove.update(fnames)

                # Also collect model names to remove (for custom model classes)
                models_to_remove = set()
                for m in misplaced_models:
                    if m["module"] == mod:
                        models_to_remove.add(m["model"])

                # Walk AST to find assignments to remove
                lines = source.splitlines(keepends=True)
                lines_to_remove = set()

                for node in ast.walk(tree):
                    if not isinstance(node, ast.ClassDef):
                        continue

                    # Check if this class has _inherit or _name matching our targets
                    class_model = None
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign):
                            for target in stmt.targets:
                                if isinstance(target, ast.Name) and target.id in ("_inherit", "_name"):
                                    if isinstance(stmt.value, ast.Constant):
                                        class_model = stmt.value.value

                    if not class_model:
                        continue

                    # Check if entire model class should be removed
                    if class_model in models_to_remove:
                        for line_no in range(node.lineno, node.end_lineno + 1):
                            lines_to_remove.add(line_no)
                        continue

                    # Remove specific field assignments
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign):
                            for target in stmt.targets:
                                if isinstance(target, ast.Name) and target.id in fields_to_remove:
                                    for line_no in range(stmt.lineno, stmt.end_lineno + 1):
                                        lines_to_remove.add(line_no)

                if not lines_to_remove:
                    continue

                # Check if removing these lines leaves an empty class
                # A class with only _inherit/_name and no field/method defs is empty
                file_has_content = False
                for node in ast.iter_child_nodes(tree):
                    if isinstance(node, ast.ClassDef):
                        for stmt in node.body:
                            stmt_lines = set(range(stmt.lineno, stmt.end_lineno + 1))
                            if stmt_lines.issubset(lines_to_remove):
                                continue
                            # _inherit and _name assignments don't count as content
                            if isinstance(stmt, ast.Assign):
                                target_names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                                if all(n in ("_inherit", "_name", "_description") for n in target_names):
                                    continue
                            file_has_content = True
                            break

                if not file_has_content:
                    await ws(f"    Removing {py_file.name} (no definitions left)")
                    py_file.unlink()
                    _remove_from_init(models_dir / "__init__.py", py_file.stem)
                else:
                    # Rewrite file without the removed lines
                    new_lines = []
                    for i, line in enumerate(lines, 1):
                        if i not in lines_to_remove:
                            new_lines.append(line)
                    # Clean up consecutive blank lines
                    cleaned = []
                    for line in new_lines:
                        if line.strip() == "" and cleaned and cleaned[-1].strip() == "":
                            continue
                        cleaned.append(line)
                    py_file.write_text("".join(cleaned))
                    removed_count = len(lines_to_remove)
                    await ws(f"    Cleaned {py_file.name} (removed {removed_count} line(s))")

        # --- Git: commit the file changes ---
        await ws("\nCommitting file changes...")
        _cfg = load_config()
        _owner = get_instance_owner(_cfg.get("instances", {}).get(instance_name, {}))
        git = f"sudo -u {_owner} git -C {addons_dir}"
        await run_cmd(f"{git} add -A", ws)
        _, status_out = await run_cmd(f"{git} status --porcelain", ws)
        if status_out.strip():
            await run_cmd(
                f'{git} commit -m "Studio Bridge: fix misplaced definitions (moved to studio_customization)"',
                ws
            )
            await run_cmd(f"{git} pull --no-edit", ws)
            await run_cmd(f"sudo -u odoo git -C {addons_dir} push", ws)
            await ws("Changes committed and pushed.")
        else:
            await ws("No file changes to commit.")

        await ws(f"\nDone. Run Studio → Module to include these definitions in {module_name}.")

    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def _remove_from_init(init_path, module_name):
    """Remove 'from . import module_name' from __init__.py."""
    if not init_path.exists():
        return
    lines = init_path.read_text().splitlines(keepends=True)
    new_lines = [l for l in lines if f"import {module_name}" not in l]
    # If nothing meaningful remains, write empty file
    if all(l.strip() == "" for l in new_lines):
        init_path.write_text("")
    else:
        init_path.write_text("".join(new_lines))


def get_custom_addons_info(config):
    result = {}
    for ver in ["19", "18"]:
        # Find first instance's addons dir for this version
        addons_dir = None
        for inst_name, inst in config.get("instances", {}).items():
            if inst.get("version", "19") == ver:
                candidate = Path(odoo_base_dir(ver)) / "data" / inst_name / "addons"
                if candidate.exists():
                    addons_dir = candidate
                    break
        if not addons_dir:
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


def get_instance_addons(instance_name, version):
    """Scan the deployed addons directory of a specific instance."""
    base = odoo_base_dir(version)
    addons_dir = Path(f"{base}/data/{instance_name}/addons")
    if not addons_dir.exists():
        return []
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
            })
        except Exception:
            addons.append({
                "name": addon_path.name,
                "display_name": addon_path.name,
                "version": "?",
                "summary": "(manifest parse error)",
            })
    return addons


def _t(value):
    """Extract a plain string from a possibly-translated JSONB dict."""
    if isinstance(value, dict):
        return value.get("en_US") or next(iter(value.values()), "")
    return value or ""


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
        attrs["string"] = _t(field["field_description"])

    if ftype == "selection":
        sel_key = (field["model"], fname)
        options = selections_map.get(sel_key, [])
        if options:
            sel_list = [(s["value"], _t(s["name"])) for s in options]
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
    if field.get("translate"):
        attrs["translate"] = True

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


async def export_studio_to_module(db_name, client, version, ws_send=None, addons_dir=None):
    ws = ws_send or (lambda m: None)

    module_name = f"{client}_base"
    if addons_dir is None:
        addons_dir = Path(PLATFORM_DIR) / f"odoo{version}" / "addons"
    else:
        addons_dir = Path(addons_dir)
    module_dir = addons_dir / module_name
    models_dir = module_dir / "models"
    security_dir = module_dir / "security"
    views_dir = module_dir / "views"

    await ws(f"Connecting to database {db_name}...")
    conn = get_db_connection(db_name)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        # 1. Custom models (manual = Studio, or already owned by module)
        await ws("Querying custom models...")
        cur.execute("""
            SELECT id, model, name, info
            FROM ir_model
            WHERE state = 'manual'
               OR (model LIKE 'x\\_%%' AND id IN (
                   SELECT res_id FROM ir_model_data
                   WHERE module = %s AND model = 'ir.model'
               ))
            ORDER BY model
        """, (module_name,))
        custom_models = cur.fetchall()
        await ws(f"  Found {len(custom_models)} custom model(s)")

        # 2. Custom fields (manual = Studio, or already owned by module)
        await ws("Querying custom fields...")
        cur.execute("""
            SELECT f.id, f.name, f.field_description, f.ttype, f.state,
                   f.required, f.index, f.translate, f.relation, f.relation_field,
                   f.relation_table, f.column1, f.column2, f.on_delete,
                   m.model, m.state as model_state
            FROM ir_model_fields f
            JOIN ir_model m ON f.model_id = m.id
            WHERE (f.related IS NULL OR f.related = '')
              AND (f.state = 'manual'
                   OR (f.name LIKE 'x\\_%%' AND f.id IN (
                       SELECT res_id FROM ir_model_data
                       WHERE module = %s AND model = 'ir.model.fields'
                   )))
            ORDER BY m.model, f.name
        """, (module_name,))
        all_fields = cur.fetchall()
        await ws(f"  Found {len(all_fields)} custom field(s)")

        # 3. Selection options
        await ws("Querying selection options...")
        cur.execute("""
            SELECT f.model, f.name as field_name, s.value, s.name, s.sequence
            FROM ir_model_fields_selection s
            JOIN ir_model_fields f ON s.field_id = f.id
            WHERE f.state = 'manual'
               OR (f.name LIKE 'x\\_%%' AND f.id IN (
                   SELECT res_id FROM ir_model_data
                   WHERE module = %s AND model = 'ir.model.fields'
               ))
            ORDER BY f.model, f.name, s.sequence
        """, (module_name,))
        selections_raw = cur.fetchall()
        selections_map = {}
        for s in selections_raw:
            key = (s["model"], s["field_name"])
            selections_map.setdefault(key, []).append(s)
        await ws(f"  Found {len(selections_raw)} selection option(s)")

        # 4. Access rules for custom models (deduplicated by model+group+perms)
        await ws("Querying access rules...")
        cur.execute("""
            SELECT DISTINCT ON (m.model, a.group_id, a.perm_read, a.perm_write, a.perm_create, a.perm_unlink)
                   a.name, m.model,
                   COALESCE(d.module || '.' || d.name, '') as group_xmlid,
                   a.perm_read, a.perm_write, a.perm_create, a.perm_unlink
            FROM ir_model_access a
            JOIN ir_model m ON a.model_id = m.id
            LEFT JOIN ir_model_data d ON d.model = 'res.groups' AND d.res_id = a.group_id
            WHERE m.state = 'manual'
               OR (m.model LIKE 'x\\_%%' AND m.id IN (
                   SELECT res_id FROM ir_model_data
                   WHERE module = %s AND model = 'ir.model'
               ))
            ORDER BY m.model, a.group_id, a.perm_read, a.perm_write, a.perm_create, a.perm_unlink, a.name
        """, (module_name,))
        access_rules = cur.fetchall()
        await ws(f"  Found {len(access_rules)} access rule(s)")

        # 5. Studio views (studio_customization or already owned by module)
        await ws("Querying custom views...")
        cur.execute("""
            SELECT v.id, v.name, v.model, v.type, v.arch_db, v.inherit_id,
                   v.priority, v.active,
                   d.name as xmlid_name, d.module as xmlid_module
            FROM ir_ui_view v
            JOIN ir_model_data d ON d.model = 'ir.ui.view' AND d.res_id = v.id
            WHERE d.module = 'studio_customization'
               OR d.module = %s
            ORDER BY v.model, v.name
        """, (module_name,))
        studio_views = cur.fetchall()
        await ws(f"  Found {len(studio_views)} custom view(s)")

        # Group fields by model
        fields_by_model = {}
        for f in all_fields:
            fields_by_model.setdefault(f["model"], []).append(f)

        custom_model_names = {m["model"] for m in custom_models}
        inherited_models = {
            model for model in fields_by_model
            if model not in custom_model_names
        }

        # Determine dependencies (from inherited models)
        depends = set()
        for model in inherited_models:
            dep = STANDARD_MODEL_MODULES.get(model)
            if dep:
                depends.add(dep)

        # Also collect dependencies from view parent modules
        for view in studio_views:
            if view.get("inherit_id"):
                cur.execute("""
                    SELECT module FROM ir_model_data
                    WHERE model = 'ir.ui.view' AND res_id = %s LIMIT 1
                """, (view["inherit_id"],))
                row = cur.fetchone()
                if row and row["module"] not in ("studio_customization", module_name):
                    depends.add(row["module"])

        depends.discard("base")
        depends = sorted(depends)
        if not depends:
            depends = ["base"]

        # Generate module files (clean previous output first)
        await ws(f"\nGenerating module {module_name}...")

        if module_dir.exists():
            shutil.rmtree(module_dir)
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
                f'    _description = "{_t(model["name"])}"',
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
            # Build model external ID references. Custom models (x_*) use
            # module-relative IDs (model_x_abschluss) since the module defines
            # them. Standard models use their existing external IDs.
            model_xmlid_map = {}
            for rule in access_rules:
                model_name = rule["model"]
                if model_name not in model_xmlid_map:
                    if model_name.startswith("x_"):
                        model_xmlid_map[model_name] = f"model_{model_name.replace('.', '_')}"
                    else:
                        cur.execute("""
                            SELECT module, name FROM ir_model_data
                            WHERE model = 'ir.model' AND res_id = (
                                SELECT id FROM ir_model WHERE model = %s LIMIT 1
                            ) LIMIT 1
                        """, (model_name,))
                        row = cur.fetchone()
                        if row:
                            model_xmlid_map[model_name] = f"{row['module']}.{row['name']}"
                        else:
                            model_xmlid_map[model_name] = f"model_{model_name.replace('.', '_')}"

            csv_lines = ["id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink"]
            for rule in access_rules:
                model_under = rule["model"].replace(".", "_")
                rule_id = f"access_{model_under}_{rule['name'].replace(' ', '_').replace('.', '_').lower()}"
                model_ref = model_xmlid_map[rule["model"]]
                group_ref = rule["group_xmlid"] or ""
                r = int(rule["perm_read"])
                w = int(rule["perm_write"])
                c = int(rule["perm_create"])
                u = int(rule["perm_unlink"])
                csv_lines.append(f"{rule_id},{_t(rule['name'])},{model_ref},{group_ref},{r},{w},{c},{u}")
            (security_dir / "ir.model.access.csv").write_text("\n".join(csv_lines) + "\n")
            await ws(f"  security/ir.model.access.csv ({len(access_rules)} rules)")

        # views/studio_views.xml
        if studio_views:
            # Build lookup: for views whose parent has no ir_model_data,
            # find a matching base view in our export set (same model+type, no inherit_id)
            export_base_views = {}
            for v in studio_views:
                if not v.get("inherit_id"):
                    export_base_views[(v["model"], v["type"])] = v["xmlid_name"]

            # Sort: base views first, then inheritance views
            sorted_views = sorted(studio_views, key=lambda v: (1 if v.get("inherit_id") else 0, v["model"], v["name"]))

            xml_lines = ['<?xml version="1.0" encoding="utf-8"?>', "<odoo>"]
            skipped = 0
            for view in sorted_views:
                arch = _t(view["arch_db"])

                # Resolve inherit_id
                parent_ref = None
                if view.get("inherit_id"):
                    cur.execute("""
                        SELECT module || '.' || name FROM ir_model_data
                        WHERE model = 'ir.ui.view' AND res_id = %s LIMIT 1
                    """, (view["inherit_id"],))
                    parent_row = cur.fetchone()
                    if parent_row:
                        parent_xmlid = list(parent_row.values())[0]
                        # If parent is in our own module, use local ref
                        if parent_xmlid.startswith("studio_customization.") or parent_xmlid.startswith(f"{module_name}."):
                            local_name = parent_xmlid.split(".", 1)[1]
                            if any(v["xmlid_name"] == local_name for v in studio_views):
                                parent_ref = local_name
                            else:
                                parent_ref = parent_xmlid
                        else:
                            parent_ref = parent_xmlid
                    else:
                        # Parent has no ir_model_data — find matching base view in our export
                        key = (view["model"], view["type"])
                        if key in export_base_views:
                            parent_ref = export_base_views[key]
                            await ws(f"  Re-parenting {view['xmlid_name']} → {parent_ref}")
                        else:
                            await ws(f"  WARNING: Skipping {view['xmlid_name']} — parent view has no xmlid")
                            skipped += 1
                            continue

                xml_lines.append(f"")
                xml_lines.append(f"    <record id=\"{view['xmlid_name']}\" model=\"ir.ui.view\">")
                xml_lines.append(f"        <field name=\"name\">{view['name']}</field>")
                xml_lines.append(f"        <field name=\"model\">{view['model']}</field>")
                xml_lines.append(f"        <field name=\"type\">{view['type']}</field>")
                if view.get("priority") and view["priority"] != 16:
                    xml_lines.append(f"        <field name=\"priority\">{view['priority']}</field>")
                if parent_ref:
                    xml_lines.append(f'        <field name="inherit_id" ref="{parent_ref}"/>')
                xml_lines.append(f"        <field name=\"arch\" type=\"xml\">")
                arch_lines = arch.strip().splitlines()
                arch_text = textwrap.dedent("\n".join(arch_lines))
                for arch_line in arch_text.splitlines():
                    xml_lines.append(f"            {arch_line}" if arch_line.strip() else "")
                xml_lines.append(f"        </field>")
                xml_lines.append(f"    </record>")
            xml_lines.append("")
            xml_lines.append("</odoo>")
            (views_dir / "studio_views.xml").write_text("\n".join(xml_lines) + "\n")
            exported = len(studio_views) - skipped
            await ws(f"  views/studio_views.xml ({exported} views{f', {skipped} skipped' if skipped else ''})")

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
        access_records = [r for r in owned_records if r["model"] == "ir.model.access"]
        other_records = [r for r in owned_records if r["model"] not in ("ir.model.fields", "ir.model", "ir.ui.view", "ir.model.access")]

        # Revert fields to manual state (only x_ fields — Odoo constraint requires it)
        if field_records:
            field_ids = [r["res_id"] for r in field_records]
            cur.execute(
                "SELECT id FROM ir_model_fields WHERE id = ANY(%s) AND name LIKE 'x\\_%%'",
                (field_ids,)
            )
            x_field_ids = [row["id"] for row in cur.fetchall()]
            skip_count = len(field_ids) - len(x_field_ids)
            await ws(f"Reverting {len(x_field_ids)} field(s) to state=manual (skipping {skip_count} non-x_ fields)...")
            if x_field_ids:
                cur.execute(
                    "UPDATE ir_model_fields SET state = 'manual' WHERE id = ANY(%s)",
                    (x_field_ids,)
                )

        # Revert custom models to manual state (only x_ models)
        if model_records:
            model_ids = [r["res_id"] for r in model_records]
            cur.execute(
                "SELECT id FROM ir_model WHERE id = ANY(%s) AND model LIKE 'x\\_%%'",
                (model_ids,)
            )
            x_model_ids = [row["id"] for row in cur.fetchall()]
            skip_count = len(model_ids) - len(x_model_ids)
            await ws(f"Reverting {len(x_model_ids)} model(s) to state=manual (skipping {skip_count} standard models)...")
            if x_model_ids:
                cur.execute(
                    "UPDATE ir_model SET state = 'manual' WHERE id = ANY(%s)",
                    (x_model_ids,)
                )

        # For views: return ownership to studio_customization.
        # If studio_customization already has an entry (dual ownership from before
        # the fix), delete the module's entry. Otherwise transfer it back.
        if view_records:
            view_data_ids = [r["id"] for r in view_records]
            cur.execute("""
                DELETE FROM ir_model_data d
                WHERE d.id = ANY(%s)
                  AND EXISTS (
                      SELECT 1 FROM ir_model_data s
                      WHERE s.module = 'studio_customization' AND s.model = 'ir.ui.view'
                        AND s.name = d.name
                  )
            """, (view_data_ids,))
            deleted_views = cur.rowcount
            cur.execute("""
                UPDATE ir_model_data SET module = 'studio_customization'
                WHERE id = ANY(%s)
            """, (view_data_ids,))
            transferred_views = cur.rowcount
            await ws(f"  Views: {deleted_views} returned (dual ownership), {transferred_views} transferred back")

        # Keep access rules alive (don't delete ir_model_access records) so they
        # survive for re-export. Only delete the ir_model_data ownership entries.
        if access_records:
            access_data_ids = [r["id"] for r in access_records]
            cur.execute("DELETE FROM ir_model_data WHERE id = ANY(%s)", (access_data_ids,))
            await ws(f"  Access rules: released ownership of {len(access_data_ids)} rule(s)")

        # Handle field/model ownership: if studio_customization also owns the
        # same record, delete the module's duplicate. Otherwise transfer back
        # to studio_customization (these were claimed from Studio during install).
        field_model_data_ids = [r["id"] for r in field_records + model_records]
        if field_model_data_ids:
            cur.execute("""
                DELETE FROM ir_model_data d
                WHERE d.id = ANY(%s)
                  AND EXISTS (
                      SELECT 1 FROM ir_model_data s
                      WHERE s.module = 'studio_customization'
                        AND s.model = d.model AND s.res_id = d.res_id
                  )
            """, (field_model_data_ids,))
            deleted = cur.rowcount
            cur.execute("""
                UPDATE ir_model_data SET module = 'studio_customization'
                WHERE id = ANY(%s)
            """, (field_model_data_ids,))
            transferred = cur.rowcount
            await ws(f"  Ownership: {deleted} returned to Studio (inherited), {transferred} transferred back (custom models)")

        # Delete other ownership records (NOT access rules — handled above)
        other_data_ids = [r["id"] for r in other_records]
        if other_data_ids:
            await ws(f"Removing {len(other_data_ids)} other ownership record(s)...")
            cur.execute(
                "DELETE FROM ir_model_data WHERE id = ANY(%s)",
                (other_data_ids,)
            )

        # Remove module record and its ir_model_data entry (under 'base' module)
        await ws(f"Removing {module_name} from module registry...")
        cur.execute(
            "DELETE FROM ir_model_data WHERE model = 'ir.module.module' AND res_id = (SELECT id FROM ir_module_module WHERE name = %s)",
            (module_name,)
        )
        cur.execute(
            "DELETE FROM ir_module_module WHERE name = %s",
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

    # Clean up generated module from all instance addons and push removal
    config = load_config()
    pushed = False
    for inst_name, inst in config.get("instances", {}).items():
        if inst.get("client") == client:
            inst_addons = Path(odoo_base_dir(inst["version"])) / "data" / inst_name / "addons"
            mod_path = inst_addons / module_name
            if mod_path.exists():
                shutil.rmtree(mod_path)
                await ws(f"Removed {mod_path}")
            # Git commit/push from first instance that has a repo
            if not pushed and (inst_addons / ".git").exists():
                import subprocess
                _owner = get_instance_owner(inst)
                subprocess.run(["sudo", "-u", _owner, "git", "-C", str(inst_addons), "add", "-A"], capture_output=True, timeout=10)
                subprocess.run(
                    ["sudo", "-u", _owner, "git", "-C", str(inst_addons), "commit", "-m", f"Revert: remove {module_name}"],
                    capture_output=True, timeout=10,
                )
                subprocess.run(["sudo", "-u", _owner, "git", "-C", str(inst_addons), "pull", "--no-edit"], capture_output=True, timeout=30)
                subprocess.run(["sudo", "-u", "odoo", "git", "-C", str(inst_addons), "push"], capture_output=True, timeout=30)
                pushed = True
                await ws(f"Pushed module removal to git repo")


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

    instance_addons = f"{base}/data/{instance_name}/addons"

    flag = "-i" if operation == "install" else "-u"
    op_label = "Installing" if operation == "install" else "Upgrading"

    await ws(f"{op_label} {module_name} on {instance_name}...")

    # Stop instance
    await ws(f"Stopping {service}...")
    await run_cmd(f"systemctl stop {service}", ws)

    # Commit and push addons changes to shared repo
    owner = get_instance_owner(instance)
    if Path(f"{instance_addons}/.git").exists():
        await ws("Committing addons to git repo...")
        await run_cmd(f"sudo -u {owner} git -C {instance_addons} add -A", ws)
        await run_cmd(
            f"sudo -u {owner} git -C {instance_addons} diff --cached --quiet || "
            f"sudo -u {owner} git -C {instance_addons} commit -m 'Studio Bridge: {op_label.lower()} {module_name}'",
            ws,
        )
        await run_cmd(f"sudo -u {owner} git -C {instance_addons} pull --no-edit", ws)
        await run_cmd(f"sudo -u odoo git -C {instance_addons} push", ws)

    # Pre-install: reclaim studio_customization records and reset states so
    # odoo-bin can properly initialize the module. Without this:
    # - Odoo's _reflect skips creating ir_model_data, breaking CSV references
    # - Manual-state models aren't registered in the ORM registry, breaking views
    conn = get_db_connection(db_name)
    cur = conn.cursor()
    cur.execute("""
        UPDATE ir_model_data SET module = %s
        WHERE module = 'studio_customization'
          AND model IN ('ir.ui.view', 'ir.model', 'ir.model.fields')
    """, (module_name,))
    pre_count = cur.rowcount

    # Set custom models and their fields to state='base' so Odoo's init_models
    # registers them in the ORM registry (manual-state models are treated as
    # dynamic/Studio models and may not be fully initialized during module load).
    cur.execute("UPDATE ir_model SET state = 'base' WHERE state = 'manual' AND model LIKE 'x\\_%%'")
    pre_models = cur.rowcount
    cur.execute("""
        UPDATE ir_model_fields SET state = 'base'
        WHERE state = 'manual' AND name LIKE 'x\\_%%'
          AND id IN (SELECT res_id FROM ir_model_data WHERE module = %s AND model = 'ir.model.fields')
    """, (module_name,))
    pre_fields = cur.rowcount

    if pre_count or pre_models:
        await ws(f"Pre-install: reclaimed {pre_count} record(s), reset {pre_models} model(s) + {pre_fields} field(s) to base")
    conn.commit()
    cur.close()
    conn.close()

    # Run odoo-bin (--logfile= forces output to stdout instead of conf logfile).
    # odoo-bin may exit non-zero on warnings even if the install succeeds,
    # so we catch errors and check module state before proceeding.
    odoo_bin = f"{base}/odoo/odoo-bin"
    await ws(f"Running odoo-bin {flag} {module_name}...")
    odoo_ok = True
    try:
        await run_cmd(
            f"su - odoo -s /bin/bash -c '{base}/venv/bin/python {odoo_bin} "
            f"-c {conf_path} {flag} {module_name} --stop-after-init "
            f"--no-http --logfile= 2>&1'",
            ws,
        )
    except RuntimeError as e:
        await ws(f"odoo-bin exited with error: {e}")
        odoo_ok = False

    # Post-install: claim ownership and clean up dual ir_model_data entries.
    # Odoo's install creates ir_model_data for the module alongside existing
    # studio_customization entries, but doesn't take full ownership of custom
    # model fields/models — we need to fix that up.
    conn = get_db_connection(db_name)
    cur = conn.cursor()

    # Verify module actually got installed
    cur.execute("SELECT state FROM ir_module_module WHERE name = %s", (module_name,))
    mod_row = cur.fetchone()
    if not mod_row or mod_row[0] != "installed":
        # Clean asset cache to prevent broken white page on next load
        cur.execute("DELETE FROM ir_attachment WHERE url LIKE '/web/assets/%%' OR url LIKE '/web/bundle/%%'")
        cleaned_assets = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        await ws(f"Cleared {cleaned_assets} cached asset bundle(s)")
        await ws(f"Starting {service}...")
        await run_cmd(f"systemctl start {service}", ws)
        if not odoo_ok:
            raise RuntimeError(f"odoo-bin failed and module {module_name} is not installed")
        raise RuntimeError(f"Module {module_name} state is {mod_row[0] if mod_row else 'not found'}")

    if not odoo_ok:
        await ws(f"Module {module_name} is installed despite exit code — proceeding with post-install")

    await ws("Post-install: claiming ownership...")

    # Step 1: Clean up dual ownership FIRST — delete studio_customization entries
    # where the module already has its own entry (from odoo-bin). This must happen
    # before claiming, otherwise the claim UPDATEs create duplicate module entries.
    cur.execute("""
        DELETE FROM ir_model_data s
        USING ir_model_data m
        WHERE s.module = 'studio_customization'
          AND m.module = %s
          AND s.model = m.model
          AND s.res_id = m.res_id
    """, (module_name,))
    cleaned_duals = cur.rowcount

    # Step 2: Claim remaining studio_customization entries for custom model
    # fields/models (where odoo-bin didn't create its own entry).
    cur.execute("""
        UPDATE ir_model_data SET module = %s
        WHERE module = 'studio_customization'
          AND model = 'ir.model.fields'
          AND res_id IN (
              SELECT f.id FROM ir_model_fields f
              JOIN ir_model m ON f.model_id = m.id
              WHERE m.model LIKE 'x\\_%%' AND f.name LIKE 'x\\_%%'
          )
    """, (module_name,))
    claimed_fields = cur.rowcount

    cur.execute("""
        UPDATE ir_model_data SET module = %s
        WHERE module = 'studio_customization'
          AND model = 'ir.model'
          AND res_id IN (
              SELECT id FROM ir_model WHERE model LIKE 'x\\_%%'
          )
    """, (module_name,))
    claimed_models = cur.rowcount

    # Step 3: Set all module-owned fields and models to state=base
    cur.execute("""
        UPDATE ir_model_fields SET state = 'base'
        WHERE state = 'manual'
          AND id IN (
              SELECT res_id FROM ir_model_data
              WHERE module = %s AND model = 'ir.model.fields'
          )
    """, (module_name,))

    cur.execute("""
        UPDATE ir_model SET state = 'base'
        WHERE state = 'manual'
          AND id IN (
              SELECT res_id FROM ir_model_data
              WHERE module = %s AND model = 'ir.model'
          )
    """, (module_name,))

    # Step 4: Clean intra-module duplicates — pre-install transferred Studio entries
    # to the module, then _reflect created new entries for the same fields/models.
    # Keep the _reflect-generated entry (standard name), delete the transferred one.
    cur.execute("""
        DELETE FROM ir_model_data d
        WHERE d.module = %s
          AND d.model IN ('ir.model.fields', 'ir.model')
          AND EXISTS (
              SELECT 1 FROM ir_model_data d2
              WHERE d2.module = d.module AND d2.model = d.model AND d2.res_id = d.res_id
                AND d2.id != d.id
          )
          AND d.id NOT IN (
              SELECT MAX(id) FROM ir_model_data
              WHERE module = %s AND model IN ('ir.model.fields', 'ir.model')
              GROUP BY model, res_id
          )
    """, (module_name, module_name))
    cleaned_intra = cur.rowcount

    # Step 5: Clean orphaned access rules for custom models — these are leftovers
    # from previous cycles (no ir_model_data) now duplicated by module's CSV rules.
    cur.execute("""
        DELETE FROM ir_model_access a
        WHERE a.model_id IN (SELECT id FROM ir_model WHERE model LIKE 'x\\_%%')
          AND NOT EXISTS (
              SELECT 1 FROM ir_model_data d
              WHERE d.model = 'ir.model.access' AND d.res_id = a.id
          )
    """)
    cleaned_rules = cur.rowcount

    conn.commit()
    cur.close()
    conn.close()
    await ws(f"  Cleaned {cleaned_duals} dual(s) + {cleaned_intra} intra-module dup(s), claimed {claimed_fields} field(s) + {claimed_models} model(s), removed {cleaned_rules} orphan rule(s)")

    # Start instance
    await ws(f"Starting {service}...")
    await run_cmd(f"systemctl start {service}", ws)

    await ws(f"\n{module_name} {operation}ed successfully on {instance_name}.")
