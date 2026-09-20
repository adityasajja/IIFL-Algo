"""The System page's log-out button called a route that did not exist, and got 'Method Not Allowed'."""

from atr.config.settings import get_settings


def _session_file():
    return get_settings().iifl_session_cache


def test_logging_out_forgets_the_saved_broker_session(auth_client):
    from pathlib import Path

    path = Path(_session_file())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")

    response = auth_client.post("/logout")

    assert response.status_code == 200
    assert response.json() == {"logged_out": True}
    assert not path.exists()


def test_logging_out_with_no_session_is_harmless_and_says_so(auth_client):
    response = auth_client.post("/logout")

    assert response.status_code == 200
    assert response.json() == {"logged_out": False}


def test_an_anonymous_request_cannot_drop_the_broker_session(client):
    from pathlib import Path

    path = Path(_session_file())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")

    response = client.post("/logout")

    assert response.status_code == 401
    assert path.exists()  # untouched
