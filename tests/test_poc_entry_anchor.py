"""Entry anchors to the ПОК level, not the zone's near edge (course с. 30).

«Где брать позицию: надёжнее всего брать от уровня ПОК (немного выше/ниже ...) — т.к. в
этом случае ТВХ будет на уровне». The zone boundary is only the range edge; the tradeable
level is the POC. Guard: when the volume profile peaks OUTSIDE the zone's cluster-mean
bounds, the POC is not a valid in-structure anchor and the edge is kept.
"""

from __future__ import annotations

from hunt_core.prizrak.orchestrator import _poc_entry

ZONE = {"lo": 100.0, "hi": 110.0}


def test_entry_anchors_to_poc_when_inside_zone() -> None:
    assert _poc_entry(110.0, zone=ZONE, poc_info={"poc": 104.0}) == 104.0


def test_edge_kept_when_poc_above_zone() -> None:
    assert _poc_entry(110.0, zone=ZONE, poc_info={"poc": 115.0}) == 110.0


def test_edge_kept_when_poc_below_zone() -> None:
    assert _poc_entry(100.0, zone=ZONE, poc_info={"poc": 95.0}) == 100.0


def test_edge_kept_when_no_poc() -> None:
    assert _poc_entry(110.0, zone=ZONE, poc_info={}) == 110.0


def test_poc_on_boundary_is_accepted() -> None:
    assert _poc_entry(110.0, zone=ZONE, poc_info={"poc": 110.0}) == 110.0
    assert _poc_entry(100.0, zone=ZONE, poc_info={"poc": 100.0}) == 100.0
