"""The fixtures themselves: right database, and isolation that holds even
when the code under test commits.

The second point is not theoretical. The first version of these fixtures
wrapped each test in a transaction to roll back, which looked fine while
tests only flushed and then leaked a committed row between two tests.
"""
from app.models import Customer


def test_runs_against_the_test_database(app):
    assert "auditmate_test" in app.config["SQLALCHEMY_DATABASE_URI"]
    assert "auditmate_dev" not in app.config["SQLALCHEMY_DATABASE_URI"]


def test_writes_are_visible_within_a_test(db):
    db.session.add(Customer(name="Isolation Check Pte Ltd"))
    db.session.flush()
    assert Customer.query.filter_by(name="Isolation Check Pte Ltd").count() == 1


def test_a_committed_write_does_not_survive_the_test(db):
    """The one that matters. Most services commit, so isolation has to
    survive a commit, not just a flush."""
    db.session.add(Customer(name="Committed Check Pte Ltd"))
    db.session.commit()
    assert Customer.query.filter_by(name="Committed Check Pte Ltd").count() == 1


def test_database_is_empty_again(db):
    assert Customer.query.count() == 0


def test_auth_client_can_be_used_twice_over(auth_client, db):
    """The leak showed up here: the login route commits, so the test user
    persisted and the next test's insert collided on the email index."""
    assert auth_client.get("/").status_code in (200, 302)


def test_auth_client_again(auth_client, db):
    assert auth_client.get("/").status_code in (200, 302)
