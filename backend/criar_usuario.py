#!/usr/bin/env python3
"""
Rode este script para criar seu usuário:
  python3 criar_usuario.py
"""
import bcrypt
import mysql.connector
import getpass
import os

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_USER = os.environ.get("DB_USER", "financeiro")
DB_PASS = os.environ.get("DB_PASS", "")
DB_NAME = os.environ.get("DB_NAME", "financeiro")

def main():
    print("=== Criar usuário ===")
    username = input("Username: ").strip()
    senha = getpass.getpass("Senha: ")
    senha2 = getpass.getpass("Confirme a senha: ")
    if senha != senha2:
        print("As senhas não coincidem!")
        return
    if len(senha) < 6:
        print("Senha deve ter pelo menos 6 caracteres!")
        return

    senha_hash = bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()

    db = mysql.connector.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME, autocommit=True
    )
    cur = db.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM usuarios")
        role = "admin" if cur.fetchone()[0] == 0 else "user"
        cur.execute(
            "INSERT INTO usuarios (username, senha_hash, role) VALUES (%s, %s, %s)",
            (username, senha_hash, role)
        )
        print(f"\n✓ Usuário '{username}' criado com sucesso!")
    except mysql.connector.IntegrityError:
        print(f"\n⚠ Usuário '{username}' já existe.")
    finally:
        cur.close(); db.close()

if __name__ == "__main__":
    main()
