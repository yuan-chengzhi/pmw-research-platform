from pmw_platform.world.situation import _artifact_refs, _project_content


ARTIFACT = "artifact/sha256/" + "a" * 64


def test_artifact_projection_never_silently_stops_at_an_arbitrary_node_cap() -> None:
    value = [0] * 20_001 + [ARTIFACT]
    assert _artifact_refs(value) == (ARTIFACT,)


def test_projection_names_every_omitted_top_level_field() -> None:
    projected = _project_content({
        "schema": "EXAMPLE_1",
        "title": "Visible title",
        "protocol_only": "not copied into the mathematical projection",
    })
    assert projected["title"] == "Visible title"
    assert projected["omitted_projected_content_fields"] == ["protocol_only"]
