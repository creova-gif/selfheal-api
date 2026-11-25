from selfheal_api import healthcheck


def test_healthcheck() -> None:
    assert healthcheck() == "ok"
