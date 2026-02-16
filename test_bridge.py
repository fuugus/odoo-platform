#!/usr/bin/env python3
"""Test Studio Bridge export/revert cycle end-to-end."""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "admin_panel"))

from studio_bridge import (
    export_studio_to_module,
    install_or_upgrade_module,
    convert_module_to_studio,
    get_db_connection,
)
from setup_steps import run_cmd

DB = "kaminfeger_prod"
CLIENT = "kaminfeger"
VERSION = "19"
INSTANCE = "kaminfeger_prod"
MODULE = "kaminfeger_base"


def query_stats():
    conn = get_db_connection(DB)
    cur = conn.cursor()
    stats = {}

    cur.execute("""
        SELECT COUNT(*) FROM ir_model_fields f
        WHERE f.state = 'manual' AND (f.related IS NULL OR f.related = '')
          AND NOT EXISTS (SELECT 1 FROM ir_model_data d WHERE d.module = %s AND d.model = 'ir.model.fields' AND d.res_id = f.id)
    """, (MODULE,))
    stats["fields_studio"] = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM ir_model_data d
        JOIN ir_model_fields f ON d.res_id = f.id
        WHERE d.module = %s AND d.model = 'ir.model.fields'
          AND f.name LIKE 'x\\_%%' AND (f.related IS NULL OR f.related = '')
    """, (MODULE,))
    stats["fields_module"] = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM ir_model m
        WHERE m.state = 'manual' AND NOT EXISTS (SELECT 1 FROM ir_model_data d WHERE d.module = %s AND d.model = 'ir.model' AND d.res_id = m.id)
    """, (MODULE,))
    stats["models_studio"] = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM ir_model_data d
        JOIN ir_model m ON d.res_id = m.id
        WHERE d.module = %s AND d.model = 'ir.model' AND m.model LIKE 'x\\_%%'
    """, (MODULE,))
    stats["models_module"] = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM ir_model_data d
        WHERE d.module = 'studio_customization' AND d.model = 'ir.ui.view'
          AND NOT EXISTS (SELECT 1 FROM ir_model_data m WHERE m.module = %s AND m.model = 'ir.ui.view' AND m.res_id = d.res_id)
    """, (MODULE,))
    stats["views_studio"] = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM ir_model_data WHERE module = %s AND model = 'ir.ui.view'", (MODULE,))
    stats["views_module"] = cur.fetchone()[0]

    cur.execute("SELECT COALESCE((SELECT state FROM ir_module_module WHERE name = %s), 'not_found')", (MODULE,))
    stats["module_state"] = cur.fetchone()[0]

    # Check for dual ownership (both studio_customization and module point to same record)
    for record_type, model_name in [("fields", "ir.model.fields"), ("models", "ir.model"), ("views", "ir.ui.view")]:
        cur.execute("""
            SELECT COUNT(*) FROM ir_model_data d
            WHERE d.module = 'studio_customization' AND d.model = %s
              AND EXISTS (SELECT 1 FROM ir_model_data m WHERE m.module = %s AND m.model = %s AND m.res_id = d.res_id)
        """, (model_name, MODULE, model_name))
        stats[f"{record_type}_dual"] = cur.fetchone()[0]

    cur.close()
    conn.close()
    return stats


def print_stats(label, stats):
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    print(f"  Fields:  {stats['fields_studio']} studio | {stats['fields_module']} module  (dual: {stats['fields_dual']})")
    print(f"  Models:  {stats['models_studio']} studio | {stats['models_module']} module  (dual: {stats['models_dual']})")
    print(f"  Views:   {stats['views_studio']} studio | {stats['views_module']} module  (dual: {stats['views_dual']})")
    print(f"  Module:  {stats['module_state']}")
    total_f = stats['fields_studio'] + stats['fields_module']
    total_m = stats['models_studio'] + stats['models_module']
    total_v = stats['views_studio'] + stats['views_module']
    print(f"  Totals:  {total_f} fields, {total_m} models, {total_v} views")


def check(label, stats, expect_studio, expect_module_state):
    """Validate stats match expectations."""
    errors = []
    if expect_studio:
        if stats["fields_module"] != 0: errors.append(f"fields_module={stats['fields_module']} (expected 0)")
        if stats["models_module"] != 0: errors.append(f"models_module={stats['models_module']} (expected 0)")
        if stats["views_module"] != 0: errors.append(f"views_module={stats['views_module']} (expected 0)")
        if stats["fields_studio"] != 31: errors.append(f"fields_studio={stats['fields_studio']} (expected 31)")
        if stats["models_studio"] != 3: errors.append(f"models_studio={stats['models_studio']} (expected 3)")
        if stats["views_studio"] != 15: errors.append(f"views_studio={stats['views_studio']} (expected 15)")
    else:
        if stats["fields_studio"] != 0: errors.append(f"fields_studio={stats['fields_studio']} (expected 0)")
        if stats["models_studio"] != 0: errors.append(f"models_studio={stats['models_studio']} (expected 0)")
        if stats["views_studio"] != 0: errors.append(f"views_studio={stats['views_studio']} (expected 0)")
        if stats["fields_module"] != 31: errors.append(f"fields_module={stats['fields_module']} (expected 31)")
        if stats["models_module"] != 3: errors.append(f"models_module={stats['models_module']} (expected 3)")
        if stats["views_module"] != 15: errors.append(f"views_module={stats['views_module']} (expected 15)")

    if stats["module_state"] != expect_module_state:
        errors.append(f"module_state={stats['module_state']} (expected {expect_module_state})")

    # No dual ownership should exist after any operation
    for t in ["fields", "models", "views"]:
        if stats[f"{t}_dual"] != 0:
            errors.append(f"{t}_dual={stats[f'{t}_dual']} (expected 0)")

    if errors:
        print(f"\n  FAIL [{label}]:")
        for e in errors:
            print(f"    - {e}")
        return False
    else:
        print(f"\n  PASS [{label}]")
        return True


async def ws_log(msg):
    # Prefix with indent for readability
    print(f"    {msg}")


async def main():
    all_pass = True

    # Initial state
    stats = query_stats()
    print_stats("INITIAL STATE", stats)
    all_pass &= check("Initial: all studio", stats, expect_studio=True, expect_module_state="uninstalled")

    # --- CYCLE 1: Export ---
    print(f"\n{'─' * 60}")
    print("  CYCLE 1: Studio → Module (export + install)")
    print(f"{'─' * 60}")
    await export_studio_to_module(DB, CLIENT, VERSION, ws_log)
    await install_or_upgrade_module(INSTANCE, MODULE, "install", ws_log)

    stats = query_stats()
    print_stats("AFTER EXPORT #1", stats)
    all_pass &= check("Export #1: all module", stats, expect_studio=False, expect_module_state="installed")

    # --- CYCLE 1: Revert ---
    print(f"\n{'─' * 60}")
    print("  CYCLE 1: Module → Studio (revert)")
    print(f"{'─' * 60}")
    await run_cmd(f"systemctl stop odoo-{INSTANCE}", ws_log)
    await convert_module_to_studio(DB, CLIENT, ws_log)

    stats = query_stats()
    print_stats("AFTER REVERT #1", stats)
    all_pass &= check("Revert #1: all studio", stats, expect_studio=True, expect_module_state="uninstalled")

    # --- CYCLE 2: Export ---
    print(f"\n{'─' * 60}")
    print("  CYCLE 2: Studio → Module (export + install)")
    print(f"{'─' * 60}")
    await export_studio_to_module(DB, CLIENT, VERSION, ws_log)
    await install_or_upgrade_module(INSTANCE, MODULE, "install", ws_log)

    stats = query_stats()
    print_stats("AFTER EXPORT #2", stats)
    all_pass &= check("Export #2: all module", stats, expect_studio=False, expect_module_state="installed")

    # --- CYCLE 2: Revert ---
    print(f"\n{'─' * 60}")
    print("  CYCLE 2: Module → Studio (revert)")
    print(f"{'─' * 60}")
    await run_cmd(f"systemctl stop odoo-{INSTANCE}", ws_log)
    await convert_module_to_studio(DB, CLIENT, ws_log)

    stats = query_stats()
    print_stats("AFTER REVERT #2", stats)
    all_pass &= check("Revert #2: all studio", stats, expect_studio=True, expect_module_state="uninstalled")

    # --- Summary ---
    print(f"\n{'=' * 60}")
    if all_pass:
        print("  ALL CHECKS PASSED")
    else:
        print("  SOME CHECKS FAILED")
    print(f"{'=' * 60}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
