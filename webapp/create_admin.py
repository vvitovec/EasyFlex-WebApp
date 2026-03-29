"""Helper script to create or update the admin user."""
from __future__ import annotations

import os
from getpass import getpass

from .app import app
from .constants import STARTING_CREDITS
from .models import User, db


def main() -> None:
	password = os.getenv("EASYFLEX_ADMIN_PASSWORD")
	if not password:
		password = getpass("Zadejte heslo pro admin účet: ")
	if not password:
		print("Heslo nesmí být prázdné.")
		return
	with app.app_context():
		user = User.query.filter_by(username="admin").first()
		if user:
			user.is_admin = True
			user.set_password(password)
			if user.credits is None:
				user.credits = STARTING_CREDITS
			action = "aktualizován"
		else:
			user = User(username="admin", is_admin=True)
			user.set_password(password)
			user.credits = STARTING_CREDITS
			db.session.add(user)
			action = "vytvořen"
		db.session.commit()
		print(f"Admin uživatel {action}.")


if __name__ == "__main__":
	main()
