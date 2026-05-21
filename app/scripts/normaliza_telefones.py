"""
normaliza_telefones.py — Normaliza obras.nivel1_telefone usando libphonenumber.

Popula nivel1_telefone_e164 + nivel1_telefone_status (valido | invalido | parse_erro).
Sobrescreve nivel1_telefone com format BR national display.
"""
import os, sys
sys.path.insert(0, "/app")
import phonenumbers
import psycopg2

DB_CONFIG = {
    "host": os.getenv("DB_HOST","db"), "port": int(os.getenv("DB_PORT","5432")),
    "dbname": os.getenv("DB_NAME","wins_hub"),
    "user": os.getenv("DB_USER","postgres"), "password": os.getenv("DB_PASSWORD",""),
}

def main():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("""
        SELECT id::text, nivel1_telefone
        FROM obras
        WHERE COALESCE(nivel1_telefone,'') <> ''
          AND nivel1_telefone_e164 IS NULL
    """)
    rows = cur.fetchall()
    print(f"\nTelefones pra normalizar: {len(rows)}\n")
    n_valido = n_invalido = n_parse_erro = 0
    for obra_id, telefone in rows:
        e164 = None
        national = telefone
        status = "invalido"
        try:
            p = phonenumbers.parse(telefone, "BR")
            if phonenumbers.is_valid_number(p):
                e164 = phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164)
                national = phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.NATIONAL)
                status = "valido"
                n_valido += 1
            else:
                n_invalido += 1
        except phonenumbers.NumberParseException:
            status = "parse_erro"
            n_parse_erro += 1
        cur.execute("""
            UPDATE obras
               SET nivel1_telefone = %s,
                   nivel1_telefone_e164 = %s,
                   nivel1_telefone_status = %s
             WHERE id = %s
        """, (national, e164, status, obra_id))
    conn.commit()
    print(f"  Válidos:      {n_valido}")
    print(f"  Inválidos:    {n_invalido}")
    print(f"  Parse erro:   {n_parse_erro}")

if __name__ == "__main__":
    main()
