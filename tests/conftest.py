"""Test fixtures.

ALWAYS ITS OWN DATABASE. `TestConfig` points at `auditmate_test` and is not
read from the environment, because the one thing a test suite must never do
to this application is touch a real engagement. `auditmate_dev` holds the
developer's working clients and the VPS holds the firm's; a fixture that
picked up `DATABASE_URL` would silently run against whichever of those the
shell happened to be configured for.

Schema is created once per session and every test runs inside a transaction
that is rolled back, so tests cannot see each other's writes and the
database is left as it was found.
"""
import os

import pytest
from sqlalchemy import text

# Set before `app.config` is imported: Config reads DATABASE_URL at class
# definition time, so an override applied later would arrive too late.
os.environ["DATABASE_URL"] = (
    "postgresql+psycopg://postgres:San%24vim03@localhost:5432/auditmate_test")

from app import create_app                                       # noqa: E402
from app.config import Config                                    # noqa: E402
from app.extensions import db as _db                             # noqa: E402


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = (
        "postgresql+psycopg://postgres:San%24vim03@localhost:5432/auditmate_test")
    # The forms are not what is under test, and a token per request would
    # make every POST here about CSRF rather than about the behaviour.
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "testing-only"


@pytest.fixture(scope="session")
def app():
    app = create_app(TestConfig)
    with app.app_context():
        _db.create_all()
        yield app


@pytest.fixture()
def db(app):
    """An empty database per test.

    Emptied by truncation AFTER each test rather than by wrapping it in a
    transaction to roll back. The obvious outer-transaction trick does not
    hold here: Flask-SQLAlchemy resolves a session's connection through its
    own `get_bind()`, so a session `bind=` is bypassed and any code that
    commits - which most services do, and the login route does - writes
    through for real. That failed silently in the safe direction at first
    (tests that only flushed looked isolated) and then showed up as a
    duplicate user between two tests. Truncation is correct whatever the
    code under test commits, and on empty tables it costs almost nothing.
    """
    with app.app_context():
        try:
            yield _db
        finally:
            _db.session.remove()
            tables = ", ".join(f'"{t.name}"' for t in _db.metadata.sorted_tables)
            if tables:
                with _db.engine.begin() as connection:
                    connection.execute(
                        text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture()
def client(app, db):
    return app.test_client()


@pytest.fixture()
def auth_client(app, db):
    """A signed-in client. Every view here is @login_required, so a test
    hitting a route without this gets a redirect to the login page rather
    than the thing it meant to assert about."""
    from app.models import User

    user = User(name="Test Auditor", email="tester@example.com", role="partner")
    user.set_password("testing-password")
    db.session.add(user)
    db.session.flush()

    test_client = app.test_client()
    response = test_client.post("/login", data={
        "email": "tester@example.com", "password": "testing-password"},
        follow_redirects=False)
    assert response.status_code in (301, 302), "login did not succeed"
    return test_client
