"""Authentication blueprint and login manager setup."""
from __future__ import annotations

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import (
	login_user,
	logout_user,
	login_required,
	current_user,
	LoginManager,
)

from .models import User

auth_bp = Blueprint("auth", __name__)
login_manager = LoginManager()
login_manager.login_view = "auth.login"


@login_manager.user_loader
def load_user(user_id: str) -> User | None:  # pragma: no cover - simple loader
	try:
		return User.query.get(int(user_id))
	except Exception:
		return None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
	if current_user.is_authenticated:
		return redirect(url_for("dashboard"))
	error = None
	if request.method == "POST":
		username = request.form.get("username", "").strip()
		password = request.form.get("password", "")
		user = User.query.filter_by(username=username).first()
		if user and user.check_password(password):
			login_user(user)
			next_page = request.args.get("next") or url_for("dashboard")
			return redirect(next_page)
		error = "Neplatné přihlašovací údaje."
		flash(error, "danger")
	return render_template("login.html", error=error)


@auth_bp.route("/logout")
@login_required
def logout():
	logout_user()
	return redirect(url_for("auth.login"))


def init_auth(app) -> None:
	"""Attach login manager and register blueprint."""
	login_manager.init_app(app)
	app.register_blueprint(auth_bp)
