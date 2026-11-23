"""Create or update an application user (passwords are stored only as PBKDF2 hashes).

    python -m scripts.manage_users add <username> <role> <password> [--name "Display Name"] [--supplier SUP-001]
    python -m scripts.manage_users list
    python -m scripts.manage_users disable <username>

Roles: ADMIN | PROCUREMENT | QUALITY | VIEWER | SUPPLIER (SUPPLIER needs --supplier).
Usernames are case-insensitive (stored lower-case; sign-in lower-cases what is typed).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.auth import ROLES, hash_password  # noqa: E402
from app.db import SessionLocal, User, init_db  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("username"); a.add_argument("role"); a.add_argument("password")
    a.add_argument("--name"); a.add_argument("--supplier")
    sub.add_parser("list")
    d = sub.add_parser("disable"); d.add_argument("username")
    args = ap.parse_args()
    init_db()
    with SessionLocal() as s:
        if args.cmd == "add":
            role = args.role.upper()
            if role not in ROLES:
                raise SystemExit(f"role must be one of {', '.join(ROLES)}")
            if role == "SUPPLIER" and not args.supplier:
                raise SystemExit("SUPPLIER users need --supplier SUP-xxx")
            un = args.username.strip().lower()
            u = s.get(User, un)
            if u:
                u.role, u.password_hash, u.active = role, hash_password(args.password), True
                u.display_name = args.name or u.display_name
                u.supplier_id = args.supplier if role == "SUPPLIER" else None
                verb = "updated"
            else:
                s.add(User(username=un, display_name=args.name or args.username.strip(), role=role,
                           supplier_id=args.supplier if role == "SUPPLIER" else None, password_hash=hash_password(args.password)))
                verb = "created"
            s.commit()
            print(f"{verb} {un} ({role})" + ("  ! weak password: use 8+ characters in production" if len(args.password) < 8 else ""))
        elif args.cmd == "disable":
            u = s.get(User, args.username.strip().lower())
            if not u:
                raise SystemExit("no such user")
            u.active = False
            s.commit()
            print(f"disabled {u.username}")
        else:
            for u in s.query(User).order_by(User.role, User.username):
                print(f"  {u.username:<26} {u.role:<12} {'active' if u.active else 'DISABLED':<9} {u.display_name}")


if __name__ == "__main__":
    main()
