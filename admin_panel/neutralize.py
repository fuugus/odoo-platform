"""Reversible, idempotent Odoo database neutralization."""
import psycopg2


def _get_conn(db_name):
    return psycopg2.connect(dbname=db_name, user="odoo")


def is_neutralized(db_name: str) -> bool:
    try:
        conn = _get_conn(db_name)
        with conn.cursor() as cr:
            cr.execute(
                "SELECT value FROM ir_config_parameter "
                "WHERE key = 'database.is_neutralized'"
            )
            row = cr.fetchone()
        conn.close()
        return row is not None and row[0].lower() in ("true", "1")
    except Exception:
        return False


def neutralize_db(db_name: str) -> dict:
    """Neutralize a database. Returns stats dict."""
    conn = _get_conn(db_name)
    stats = {}
    with conn.cursor() as cr:
        # Config flag
        cr.execute(
            "INSERT INTO ir_config_parameter (key, value) "
            "VALUES ('database.is_neutralized', 'true') "
            "ON CONFLICT (key) DO UPDATE SET value = 'true'"
        )

        # Activate banner views
        cr.execute(
            "UPDATE ir_ui_view SET active = true "
            "WHERE key IN ('web.neutralize_banner', 'website.neutralize_ribbon') "
            "AND active = false"
        )
        stats["banners"] = cr.rowcount

        # Crons: mark active ones with [neutralized] suffix, deactivate
        # Skip autovacuum (Odoo's official neutralize also skips it)
        cr.execute(
            "UPDATE ir_cron "
            "SET cron_name = cron_name || ' [neutralized]', active = false "
            "WHERE active = true "
            "AND cron_name NOT LIKE '%% [neutralized]' "
            "AND id NOT IN ("
            "  SELECT res_id FROM ir_model_data "
            "  WHERE model = 'ir.cron' AND name = 'autovacuum_job' AND module = 'base'"
            ")"
        )
        stats["crons"] = cr.rowcount

        # Mail servers: mark active ones with [neutralized] prefix, deactivate
        cr.execute(
            "UPDATE ir_mail_server "
            "SET name = '[neutralized] ' || name, active = false "
            "WHERE active = true AND name NOT LIKE '[neutralized] %%'"
        )
        stats["mail_servers"] = cr.rowcount

        # Clean up dummy server from official odoo-bin neutralize
        cr.execute(
            "DELETE FROM ir_mail_server "
            "WHERE name = 'neutralization - disable emails'"
        )


    conn.commit()
    conn.close()
    return stats


def deneutralize_db(db_name: str) -> dict:
    """Remove neutralization from a database. Returns stats dict."""
    conn = _get_conn(db_name)
    stats = {}
    with conn.cursor() as cr:
        # Config flag
        cr.execute(
            "INSERT INTO ir_config_parameter (key, value) "
            "VALUES ('database.is_neutralized', 'false') "
            "ON CONFLICT (key) DO UPDATE SET value = 'false'"
        )

        # Deactivate banner views
        cr.execute(
            "UPDATE ir_ui_view SET active = false "
            "WHERE key IN ('web.neutralize_banner', 'website.neutralize_ribbon') "
            "AND active = true"
        )
        stats["banners"] = cr.rowcount

        # Crons: restore those with [neutralized] marker
        cr.execute(
            "UPDATE ir_cron "
            "SET cron_name = REPLACE(cron_name, ' [neutralized]', ''), active = true "
            "WHERE cron_name LIKE '%% [neutralized]'"
        )
        stats["crons"] = cr.rowcount

        # Mail servers: restore those with [neutralized] prefix
        cr.execute(
            "UPDATE ir_mail_server "
            "SET name = REPLACE(name, '[neutralized] ', ''), active = true "
            "WHERE name LIKE '[neutralized] %%'"
        )
        stats["mail_servers"] = cr.rowcount

        # Clean up dummy server from official odoo-bin neutralize
        cr.execute(
            "DELETE FROM ir_mail_server "
            "WHERE name = 'neutralization - disable emails'"
        )


    conn.commit()
    conn.close()
    return stats
