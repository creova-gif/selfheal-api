from selfheal_api import healthcheck


def test_healthcheck() -> None:
    assert healthcheck() == "ok"


def test_public_sdk_exports() -> None:
    import selfheal_api

    for symbol in selfheal_api.__all__:
        assert getattr(selfheal_api, symbol) is not None
