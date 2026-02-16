# Studio Bridge

Converts Odoo Studio customizations into proper Python modules and back.
Lives in `admin_panel/studio_bridge.py`.

## What it does

Odoo Studio stores field definitions, custom models, and views as dynamic
database records under the `studio_customization` module. Studio Bridge
exports these into a standard Odoo addon (Python models, CSV access rules,
XML views) that can be version-controlled and deployed like any other module.
It can also revert — converting the module back to Studio state so users can
keep editing in Studio's visual editor.

## Key concepts

### ir_model_data ownership

Every Odoo record that belongs to a module has a row in `ir_model_data`
linking `(module, name)` to `(model, res_id)`. Studio uses
`module='studio_customization'` with UUID-based names. When we install our
generated module, ownership must transfer cleanly — no duplicates, no orphans.

### Field/model state

- `state='manual'` — Studio-created, loaded dynamically via registry reload
- `state='base'` — code-defined, loaded during module init via `init_models`

This distinction matters: Odoo's `init_models` may not fully register
manual-state models during module install. Custom models and their fields
must be set to `state='base'` before `odoo-bin -i` runs.

### Odoo's _reflect mechanism

During module install, `_reflect()` creates `ir_model_data` entries for
models and fields defined in Python code. It checks by `(model, res_id)`
across ALL modules — if any entry already exists for that `res_id`, it
skips creating a new one. This is why the pre-install step must transfer
`studio_customization` entries to the module first: otherwise `_reflect`
sees the existing Studio entries and doesn't create module entries, which
breaks CSV external ID references.

For custom models, `_reflect` creates entries named
`model_{model_name_with_dots_as_underscores}` (e.g., `model_x_abschluss`).
The generated CSV must use these module-relative IDs, not the original
`studio_customization.uuid` references.

## Export flow (Studio to Module)

### 1. Generate module files

Reads from the database:
- Custom models (`ir_model WHERE state='manual'`)
- Studio fields (`ir_model_fields WHERE state='manual'`, excluding `related` delegation fields from `res.users`)
- Selection options (`ir_model_fields_selection`)
- Access rules (`ir_model_access` for custom models, deduplicated)
- Studio views (`ir_ui_view` owned by `studio_customization`)

Generates:
- Python model files (one per model, `_name` for custom, `_inherit` for inherited)
- `security/ir.model.access.csv` with module-relative model IDs
- `views/studio_views.xml` with local refs for same-module parent views
- `__manifest__.py` with auto-detected dependencies

### 2. Pre-install

Before `odoo-bin` runs, we prepare the database:

```sql
-- Transfer all Studio ownership to the module
UPDATE ir_model_data SET module = '{module}'
WHERE module = 'studio_customization'
  AND model IN ('ir.ui.view', 'ir.model', 'ir.model.fields');

-- Reset custom models/fields to 'base' state
UPDATE ir_model SET state = 'base' WHERE state = 'manual' AND model LIKE 'x\_%';
UPDATE ir_model_fields SET state = 'base' WHERE state = 'manual' AND name LIKE 'x\_%'
  AND id IN (SELECT res_id FROM ir_model_data WHERE module = '{module}' AND model = 'ir.model.fields');
```

Why: Without the ownership transfer, `_reflect` skips creating module
entries, breaking CSV `model_id:id` lookups. Without the state reset,
custom models aren't registered in the ORM registry, breaking view
validation.

### 3. odoo-bin install

Standard `odoo-bin -i {module} --stop-after-init`. This creates tables,
loads CSV access rules, processes XML views, and runs `_reflect`.

### 4. Post-install cleanup

After `odoo-bin`, four steps in strict order:

1. **Clean dual ownership** — Delete `studio_customization` entries where
   the module already has its own entry for the same `(model, res_id)`.
   Must happen first to prevent duplicates during claiming.

2. **Claim remaining entries** — Transfer any `studio_customization` entries
   for custom model fields/models that `_reflect` didn't create new entries
   for.

3. **Set state=base** — Ensure all module-owned fields and models are
   `state='base'`.

4. **Clean orphaned access rules** — Delete `ir_model_access` records for
   custom models that have no `ir_model_data` entry (leftovers from
   previous cycles, now replaced by the CSV-defined rules).

## Revert flow (Module to Studio)

### Critical: stop the Odoo service first

The running Odoo process monitors `base_registry_signaling` for changes.
When it detects a registry change (e.g., module set to `uninstalled`), it
reloads and drops custom model tables. This destroys data irreversibly.
Always `systemctl stop` the instance before making revert DB changes.

### Steps

1. **Revert field/model state** — Set `x_` fields and models back to
   `state='manual'`.

2. **Transfer view ownership** — Move `ir_model_data` for views back to
   `studio_customization`. If Studio already has an entry (dual ownership
   from older code), delete the module's duplicate instead.

3. **Release access rules** — Delete `ir_model_data` entries for access
   rules but keep the actual `ir_model_access` records alive. This way
   they survive for re-export.

4. **Transfer field/model ownership** — Move `ir_model_data` for fields
   and models back to `studio_customization`.

5. **Clean other records** — Delete remaining `ir_model_data` entries
   (actions, menus, constraints, etc.).

6. **Mark module uninstalled** — `UPDATE ir_module_module SET state = 'uninstalled'`.

7. **Delete generated files** — Remove the module directory from the repo.

## External ID reference conventions

### CSV (ir.model.access.csv)

Custom models use module-relative IDs that match what `_reflect` generates:
```
model_x_abschluss     (not studio_customization.some-uuid)
model_x_datei
model_x_kategorie
```

Standard models use their canonical external ID:
```
base.model_res_partner
hr.model_hr_employee
```

### XML (views)

Parent views within the same module use local refs (no module prefix):
```xml
<field name="inherit_id" ref="default_form_view_fo_a1fb3fd8-..."/>
```

Parent views from other modules use full qualified refs:
```xml
<field name="inherit_id" ref="base.view_partner_form"/>
```

Using `studio_customization.xxx` in either CSV or XML will break because
the pre-install step transfers those entries to the installing module.

## Testing

`test_bridge.py` runs 2 full export/revert cycles and checks after each step:
- 31 fields (15 inherited + 16 custom model fields), all in the expected owner
- 3 custom models, all in the expected owner
- 15 views, all in the expected owner
- 0 dual ownership entries
- Correct module state (installed/uninstalled)

## Known limitations

- **Single module per client** — Assumes all Studio customizations go into
  one `{client}_base` module. Multiple modules touching the same Studio
  fields would conflict.
- **STANDARD_MODEL_MODULES coverage** — The dependency detection only knows
  about models listed in this map. Studio fields on unlisted models (e.g.,
  `mrp.production`) would miss the `mrp` dependency in `__manifest__.py`.
- **Views without ir_model_data parents** — If a Studio view inherits from
  a view that has no external ID at all, the export skips it with a warning.
- **Arch whitespace** — `textwrap.dedent` normalizes indentation on export,
  but Odoo may store arch_db with different whitespace after install. This
  is cosmetic and doesn't affect functionality.
