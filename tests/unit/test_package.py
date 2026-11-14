from importlib import import_module


def test_subpackages_are_importable() -> None:
    import_module("multi_agent.core")
    import_module("multi_agent.adapters")
    import_module("multi_agent.store")
