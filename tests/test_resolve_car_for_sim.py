"""T145 — Unit tests: sim↔real equivalence returns correct master_id.

Asserts that the equivalence layer composes cleanly with the car
catalog: a sim-specific car identifier resolves to the *correct*
master_car_id, and that master_car_id points at the full
`CarCatalogEntry` for the right physical car.
"""

from coaching.catalog.equivalence import (
    lookup_master_car_id,
    resolve_car_by_name,
    resolve_car_for_sim,
)
from coaching.catalog.loader import (
    CarCatalogEntry,
    get_car_catalog,
)


def test_lookup_master_car_id_returns_known_pair() -> None:
    # ACC's "porsche_991ii_gt3_r" maps to the Porsche 911 GT3 R.
    master_id = lookup_master_car_id(
        provider="acc", sim_car_id="porsche_991ii_gt3_r"
    )
    assert master_id is not None
    catalog = get_car_catalog()
    assert master_id in catalog
    assert catalog[master_id].name == "porsche-911-gt3-r-2023"


def test_lookup_master_car_id_returns_none_for_unknown_sim() -> None:
    assert (
        lookup_master_car_id(
            provider="nonexistent-sim", sim_car_id="bogus"
        )
        is None
    )


def test_cross_sim_entries_share_the_same_master_car_id() -> None:
    # The killer use case: iRacing's Porsche 911 GT3 R and ACC's
    # porsche_991ii_gt3_r are the same physical car, so they must
    # resolve to the same master_car_id.
    iracing_id = lookup_master_car_id(provider="iracing", sim_car_id="64")
    acc_id = lookup_master_car_id(
        provider="acc", sim_car_id="porsche_991ii_gt3_r"
    )
    ac_id = lookup_master_car_id(
        provider="ac", sim_car_id="ks_porsche_911_gt3_r_2019"
    )
    assert iracing_id is not None
    assert iracing_id == acc_id == ac_id


def test_resolve_car_for_sim_returns_full_entry() -> None:
    entry = resolve_car_for_sim(
        provider="acc", sim_car_id="bmw_m4_gt3"
    )
    assert entry is not None
    assert isinstance(entry, CarCatalogEntry)
    assert entry.manufacturer == "BMW"
    assert entry.car_class == "GT3"
    assert entry.power_kw > 0


def test_resolve_car_for_sim_returns_none_for_unknown_pair() -> None:
    assert (
        resolve_car_for_sim(
            provider="acc", sim_car_id="not_a_car"
        )
        is None
    )


def test_resolve_car_by_name_finds_seeded_car() -> None:
    entry = resolve_car_by_name("ferrari-296-gt3-2023")
    assert entry is not None
    assert entry.name == "ferrari-296-gt3-2023"
    assert entry.master_car_id in get_car_catalog()


def test_resolve_car_by_name_returns_none_for_unknown_name() -> None:
    assert resolve_car_by_name("not-a-real-car") is None


def test_every_seeded_master_car_id_in_equivalence_is_resolvable() -> None:
    # Loose round-trip: every (provider, sim_car_id) pair in the
    # equivalence table resolves to a real entry.
    from coaching.catalog.equivalence import get_sim_car_equivalence

    catalog_ids = set(get_car_catalog().keys())
    for key, master_id in get_sim_car_equivalence().items():
        assert master_id in catalog_ids, (
            f"dangling equivalence entry: {key} → {master_id}"
        )
