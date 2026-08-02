"""The Compose Docker authority exposes only exact container inspection."""

from kdive.processes.docker_death_api import permitted_inspect_path


def test_only_exact_container_inspect_get_is_permitted() -> None:
    container_id = "a" * 64
    assert permitted_inspect_path("GET", f"/containers/{container_id}/json")
    assert not permitted_inspect_path("POST", f"/containers/{container_id}/json")
    assert not permitted_inspect_path("GET", "/containers/json")
    assert not permitted_inspect_path("GET", f"/containers/{container_id}/logs")
    assert not permitted_inspect_path("GET", f"/containers/{container_id}/archive?path=/")
    assert not permitted_inspect_path("GET", f"/containers/{container_id}/json?size=1")
