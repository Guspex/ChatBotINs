#!/usr/bin/env python3
# diagnostico.py — rode isso para identificar o problema exato
import os, sys

print("=== DIAGNÓSTICO DE CONEXÃO ===\n")

# 1. Mostra o que está sendo lido como configuração
DB_CONFIG = {
    "host":     os.getenv("PGHOST",     "localhost"),
    "port":     os.getenv("PGPORT",     "5432"),
    "dbname":   os.getenv("PGDATABASE", "oraculo_udesc"),
    "user":     os.getenv("PGUSER",     "oraculo"),
    "password": os.getenv("PGPASSWORD", "oraculo_pass"),
}

print("Configuração lida:")
for k, v in DB_CONFIG.items():
    # Mostra cada caractere e seu byte para identificar o problemático
    print(f"  {k}: {repr(v)}")
    for i, c in enumerate(v):
        if ord(c) > 127:
            print(f"    !! char especial na posição {i}: {repr(c)} (ord={ord(c)}, hex=0x{ord(c):02x})")

# 2. Testa o .env se existir
env_file = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_file):
    print(f"\n.env encontrado em: {env_file}")
    # Tenta ler com diferentes encodings
    for enc in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
        try:
            content = open(env_file, encoding=enc).read()
            print(f"  Lido com {enc}: OK")
            break
        except Exception as e:
            print(f"  Falha com {enc}: {e}")
else:
    print("\nNenhum arquivo .env encontrado.")

# 3. Tenta conexão com kwargs
print("\nTentando conectar ao PostgreSQL...")
try:
    import psycopg2
    # Garante que a senha é string ASCII pura
    senha = DB_CONFIG["password"].encode("ascii", errors="replace").decode("ascii")
    conn = psycopg2.connect(
        host     = DB_CONFIG["host"],
        port     = int(DB_CONFIG["port"]),
        dbname   = DB_CONFIG["dbname"],
        user     = DB_CONFIG["user"],
        password = senha,
    )
    conn.close()
    print("  ✓ Conexão OK!")
except Exception as e:
    print(f"  ✗ Erro: {e}")
    print(f"\n  SOLUÇÃO: mude a senha do banco para usar só letras e números (sem acentos).")
    print(f"  No PostgreSQL: ALTER USER oraculo WITH PASSWORD 'oraculo123';")
    print(f"  No .env: PGPASSWORD=oraculo123")