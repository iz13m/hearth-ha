"""The setup-axis scan's own logic and its committed baseline (#514).

The scan itself (`scripts/setup_axis_path_fields.py`) is deliberately not run here, for the reason
`test_policy.py` gives: its output is not fit to drive a denial. What is tested is the part that can
be wrong without anyone noticing, which is the comparison: a diff that reports nothing is
indistinguishable from a bump that changed nothing, so each way a row can move has a control.
"""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("setup_axis_path_fields", SCRIPTS / "setup_axis_path_fields.py")
assert spec and spec.loader
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


def rows(*items: tuple[str, bool, str]) -> list[dict]:
    return [{"component": c, "gate_calls": 1 if g else 0, "tokens": {t: 1}} for c, g, t in items]


BASE = {"tight": rows(("a", False, "file_path")), "loose": rows(("a", False, "file_path"), ("b", True, "_PATH"))}


def lines(current: dict, baseline: dict = BASE) -> dict[str, list[str]]:
    return scan.diff(baseline, current)


def test_no_difference_reports_nothing_in_either_tier() -> None:
    assert lines(BASE) == {"tight": [], "loose": []}


def test_a_new_row_is_reported_in_the_tier_it_appeared_in() -> None:
    current = {"tight": BASE["tight"], "loose": BASE["loose"] + rows(("c", False, "_path"))}
    out = lines(current)
    assert out["tight"] == []
    assert len(out["loose"]) == 1 and out["loose"][0].startswith("NEW") and " c " in out["loose"][0] and "ungated" in out["loose"][0]


def test_a_row_that_went_away_is_reported() -> None:
    current = {"tight": BASE["tight"], "loose": rows(("a", False, "file_path"))}
    out = lines(current)
    assert len(out["loose"]) == 1 and out["loose"][0].startswith("GONE") and " b " in out["loose"][0]


def test_a_flow_that_gained_or_lost_the_gate_is_reported() -> None:
    # `b` was gated by is_allowed_path and no longer is: the case a count of rows cannot see.
    current = {"tight": BASE["tight"], "loose": rows(("a", False, "file_path"), ("b", False, "_PATH"))}
    out = lines(current)
    assert len(out["loose"]) == 1 and "GATED -> ungated" in out["loose"][0]


def test_a_changed_matching_token_is_reported() -> None:
    current = {"tight": BASE["tight"], "loose": rows(("a", False, "CONF_PATH"), ("b", True, "_PATH"))}
    out = lines(current)
    assert len(out["loose"]) == 1 and "'file_path' -> 'CONF_PATH'" in out["loose"][0]


def test_two_runs_over_the_same_rows_agree_regardless_of_order() -> None:
    shuffled = {"tight": BASE["tight"], "loose": list(reversed(BASE["loose"]))}
    assert lines(shuffled) == {"tight": [], "loose": []}


BASELINE = json.loads((SCRIPTS / "setup_axis_baseline.json").read_text())


def test_the_baseline_has_both_thresholds_and_a_pinned_version() -> None:
    assert set(BASELINE) >= {"homeassistant", "tight", "loose", "read"}
    assert BASELINE["homeassistant"].count(".") == 2
    for tier in ("tight", "loose"):
        assert BASELINE[tier], f"{tier} baseline is empty"
        assert all(set(r) == {"component", "gate_calls", "tokens"} and r["tokens"] for r in BASELINE[tier])
        names = [r["component"] for r in BASELINE[tier]]
        assert names == sorted(names) and len(names) == len(set(names))


def test_every_flow_marked_read_is_in_the_loose_baseline_with_a_mechanism() -> None:
    # TIGHT is a subset of LOOSE by construction, so a read flow missing from LOOSE is a typo or a flow
    # Home Assistant removed. A `read` entry with no mechanism is a verdict with the reason stripped.
    loose = {r["component"] for r in BASELINE["loose"]}
    for name, entry in BASELINE["read"].items():
        assert name in loose, f"{name} is marked read but is not in the loose baseline"
        assert entry["mechanism"].strip() and entry["where"].strip(), f"{name} has no recorded reading"



def _fake_components(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flows: dict[str, str]) -> None:
    """Point the scan at a directory of synthetic `config_flow.py` files instead of Home Assistant's."""
    import homeassistant.components as ha_components

    root = tmp_path / "components"
    for name, source in flows.items():
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "config_flow.py").write_text(source)
    (root / "__init__.py").write_text("")
    monkeypatch.setattr(ha_components, "__file__", str(root / "__init__.py"))


FIELD = "vol.Required(CONF_FILE_PATH): str,\n"


def test_a_flow_that_gains_a_later_match_is_reported_though_its_first_match_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The case #514 exists for: an integration someone already read gains a second raw path field upstream.
    # The first match is the same token in the same place, and the gate (if any) is still there. The fields are
    # identical on purpose: a set of distinct tokens would miss a repeat.
    _fake_components(tmp_path, monkeypatch, {"read_flow": FIELD, "gated_flow": FIELD + "is_allowed_path(path)\n"})
    before = scan.scan()
    assert [r["component"] for r in before["tight"]] == ["gated_flow", "read_flow"]

    (tmp_path / "components" / "read_flow" / "config_flow.py").write_text(FIELD + FIELD)
    (tmp_path / "components" / "gated_flow" / "config_flow.py").write_text(FIELD + FIELD + "is_allowed_path(path)\n")
    out = scan.diff(before, scan.scan())

    for tier in ("tight", "loose"):
        assert len(out[tier]) == 2, f"{tier}: a second match in an already-matching flow went unreported: {out[tier]}"
        assert all(line.startswith("CHANGED") for line in out[tier])


def test_a_second_gate_call_is_reported_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Gated stays true, but a gate guarding one field says nothing about a field added beside it.
    _fake_components(tmp_path, monkeypatch, {"flow": FIELD + "is_allowed_path(a)\n"})
    before = scan.scan()
    (tmp_path / "components" / "flow" / "config_flow.py").write_text(FIELD + "is_allowed_path(a)\nis_allowed_path(b)\n")
    out = scan.diff(before, scan.scan())
    assert len(out["tight"]) == 1 and out["tight"][0].startswith("CHANGED") and "gate" in out["tight"][0]


def test_an_unchanged_synthetic_tree_reports_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_components(tmp_path, monkeypatch, {"flow": FIELD + FIELD + "is_allowed_path(a)\n"})
    assert scan.diff(scan.scan(), scan.scan()) == {"tight": [], "loose": []}


def test_the_baseline_pin_is_the_one_the_test_suite_runs_on() -> None:
    # The version stamp only; this does not run the scan and cannot drive a denial. It exists so a pin bump
    # that skips the README step does not arrive with every test green. Editing the number to get past it is a
    # hand change to the baseline in the bump's own diff, where a reviewer can see it.
    assert BASELINE["homeassistant"] == scan.installed_version(), (
        "the Home Assistant pin moved: run `.venv/bin/python scripts/setup_axis_path_fields.py`, read what it "
        "reports, record it in the baseline's `read` map, then --write-baseline (README: Bumping the Home Assistant pin)"
    )
