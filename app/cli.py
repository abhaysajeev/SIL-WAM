"""CLI commands for SIL WAM administration."""
import sys

SUPER_ADMIN_ROLE_ID = "00000000-0000-0000-0000-000000000001"


def create_superadmin():
    """
    Usage: venv/bin/python run.py create-superadmin <username> <password>

    Creates a super admin user. Email defaults to <username>@internal.
    """
    args = sys.argv[2:]
    if len(args) < 2:
        print("Usage: venv/bin/python run.py create-superadmin <username> <password>")
        sys.exit(1)

    username, password = args[0], args[1]

    if len(password) < 8:
        print("Error: password must be at least 8 characters.")
        sys.exit(1)

    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models.user import User

    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == username).first():
            print(f"Error: username '{username}' already exists.")
            sys.exit(1)

        db.add(User(
            username=username,
            email=f"{username}@internal",
            full_name=username,
            hashed_password=hash_password(password),
            role_id=SUPER_ADMIN_ROLE_ID,
            company_id=None,
            is_active=True,
            must_change_password=False,
        ))
        db.commit()
        print(f"Super admin '{username}' created.")
    except Exception as exc:
        db.rollback()
        print(f"Error: {exc}")
        sys.exit(1)
    finally:
        db.close()
