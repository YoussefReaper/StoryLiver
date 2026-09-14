"""Pytest collection rules for StoryLiver's dual-use test modules.

Most modules are executable audit scripts as well as pytest suites. Their
``test_all_*`` wrapper calls the same ``test_*`` functions pytest already
collects, in the same process, and therefore reruns stateful checks against a
populated temporary database. Keep the wrappers for ``python -m tests.foo``;
omit only those duplicate aggregators from pytest collection.
"""


def pytest_collection_modifyitems(items):
    items[:] = [item for item in items if not item.name.startswith("test_all_")]
