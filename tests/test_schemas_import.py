def test_lap_schemas_session_manifest_is_importable() -> None:
    from lap_schemas.v1.session import SessionManifest

    assert SessionManifest.__name__ == "SessionManifest"


def test_lap_schemas_data_quality_is_importable() -> None:
    from lap_schemas.v1.data_quality import DataQuality

    assert DataQuality.__name__ == "DataQuality"
