"""Admin password verification, reset, and superuser normalization."""
import psycopg2
from passlib.context import CryptContext

_crypt_ctx = CryptContext(
    schemes=["pbkdf2_sha512", "plaintext"],
    deprecated=["plaintext"],
)


def _get_conn(db_name):
    return psycopg2.connect(dbname=db_name, user="odoo")


def verify_admin_pw(db_name: str, plaintext_pw: str) -> bool:
    """Check if plaintext_pw matches the stored hash for superuser (id=2)."""
    try:
        conn = _get_conn(db_name)
        with conn.cursor() as cr:
            cr.execute("SELECT password FROM res_users WHERE id = 2")
            row = cr.fetchone()
        conn.close()
        if not row or not row[0]:
            return False
        return _crypt_ctx.verify(plaintext_pw, row[0])
    except Exception:
        return False


def reset_admin_pw(db_name: str, new_pw: str):
    """Set a new plaintext password for superuser (id=2) and normalize login/name.
    Odoo hashes the password on next login.
    """
    conn = _get_conn(db_name)
    with conn.cursor() as cr:
        cr.execute(
            "UPDATE res_users SET password = %s, login = 'admin' WHERE id = 2",
            (new_pw,),
        )
        cr.execute(
            "UPDATE res_partner SET name = 'Admin' "
            "WHERE id = (SELECT partner_id FROM res_users WHERE id = 2)"
        )
    conn.commit()
    conn.close()


def normalize_superuser(db_name: str):
    """Set login='admin' and partner name='Admin' for superuser (id=2)."""
    conn = _get_conn(db_name)
    with conn.cursor() as cr:
        cr.execute("UPDATE res_users SET login = 'admin' WHERE id = 2")
        cr.execute(
            "UPDATE res_partner SET name = 'Admin' "
            "WHERE id = (SELECT partner_id FROM res_users WHERE id = 2)"
        )
    conn.commit()
    conn.close()
