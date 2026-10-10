"""The setup axis: which Home Assistant CONFIG FLOWS take a filesystem path (#256, #514).

Why this exists: #227's classifier enumerates `*/services.yaml` — the **call** axis — while the PR
argued "two axes, because one output hides half the class". It had an instrument for one axis and a
hand-maintained list for the other, so no integration whose *config flow* takes a path could ever be
surfaced. `local_file` was found by a reviewer reading, not by the scan. This is the missing half.

**What it reads: only each component's `config_flow.py`.** A path field declared anywhere else — an
options flow in another file, a service schema, a platform, a helper imported from a library — is
invisible to it. So a clean diff means "no new match in the config flows", never "no new path
fields". `upb`, for instance, is a config-flow match whose danger sits in an external library.

It answers "does the flow mention a path-shaped field, and does that flow call `is_allowed_path`?"
It does **not** answer the question #256 actually turns on — *does the content ever surface, or is
the effect a planted configuration?* — which needs reading each component. `local_file` earned its
denial because it plants a `camera.*` entity backed by any readable file; a credential reader that
consumes a cert and never echoes it is a lower thing, and must be established rather than assumed in
either direction.

**Two thresholds, reported together, because a count is a property of a pattern.** TIGHT names
file-path fields explicitly; LOOSE adds any `_PATH` suffix and `key_file`. At `homeassistant==2026.9.3`
they give 13 and 40. Neither is "the answer", and neither number is the control. The control is the
diff against `setup_axis_baseline.json`: one row per flow per threshold, so a pin bump reports the
rows that are new, gone or changed and nothing else.

**A row records every match with its count, and how many times the flow calls `is_allowed_path`.**
A flow someone has already read can gain a second raw path field upstream: the first match is the same
token in the same place and the gate is still present, so a row holding only the first match would
report nothing. The cost is that a refactor adding one more reference to an existing constant also
reports `CHANGED`, which fails loud and means one flow gets read (Security's review of #524). Most of LOOSE's ungated rows have never been
read; the baseline's `read` map says which have, and what mechanism took each out.

Not part of `pytest`, for the reason `tests/test_policy.py` gives: this output is not fit to drive a
denial, and a suite that fails on it would be asking for a denial it cannot justify. It runs at a pin
bump (`README.md`, "Bumping the Home Assistant pin"), and what it reports is read by a person.

Usage (from `packages/ha-integration`, with the venv `scripts/test.sh` creates):

    .venv/bin/python scripts/setup_axis_path_fields.py            # diff against the baseline; exit 1 if it differs
    .venv/bin/python scripts/setup_axis_path_fields.py --list     # print every row, no comparison
    .venv/bin/python scripts/setup_axis_path_fields.py --write-baseline
                                                                  # rewrite the baseline from the installed
                                                                  # Home Assistant, keeping the `read` map

`--write-baseline` is the last step of a bump, after the diff has been read; running it first
turns the baseline into a copy of whatever the new version contains.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

BASELINE = pathlib.Path(__file__).with_name("setup_axis_baseline.json")

# Deliberately loose on the field name and reported with the matching token, so a false positive is
# visible as one rather than hidden. `zwave_js` has four matching fields (a serial port, a socket path
# handed to its add-on, a URL, and its own NVM backup file) and `transmission`'s may be an HTTP route.
# As far as anyone has read, none is a file-disclosure primitive, and the scan cannot tell you that.
PATTERNS = {
    "tight": re.compile(r"CONF_FILE_PATH|CONF_FILENAME|\bfile_path\b|\bfilename\b", re.I),
    "loose": re.compile(r"CONF_FILE_PATH|CONF_FILENAME|CONF_PATH\b|\bfile_path\b|\bfilename\b|\bkey_file\b|_PATH\b", re.I),
}
TIERS = tuple(PATTERNS)

Row = dict[str, object]  # {"component": str, "gate_calls": int, "tokens": {matched text: how many times}}


def scan() -> dict[str, list[Row]]:
    """Every config flow matching each threshold, from the installed Home Assistant."""
    import homeassistant.components as ha_components  # imported here so the diff logic needs no Home Assistant

    root = pathlib.Path(ha_components.__file__).parent
    out: dict[str, list[Row]] = {tier: [] for tier in TIERS}
    for component in sorted(p for p in root.iterdir() if p.is_dir()):
        config_flow = component / "config_flow.py"
        if not config_flow.exists():
            continue
        try:
            source = config_flow.read_text(errors="ignore")
        except OSError:
            continue
        for tier, pattern in PATTERNS.items():
            tokens: dict[str, int] = {}
            for m in pattern.finditer(source):
                tokens[m.group(0)] = tokens.get(m.group(0), 0) + 1
            if tokens:
                tokens = dict(sorted(tokens.items()))
                out[tier].append({"component": component.name, "gate_calls": source.count("is_allowed_path"), "tokens": tokens})
    return out


def gated(row: Row) -> bool:
    """The flow calls `is_allowed_path` somewhere. It does not say which field that call guards."""
    return int(row["gate_calls"]) > 0  # type: ignore[arg-type]


def summary(row: Row) -> str:
    return ", ".join(f"{t!r}" + (f" x{n}" if n > 1 else "") for t, n in row["tokens"].items())  # type: ignore[attr-defined]


def installed_version() -> str:
    import homeassistant.const as const

    return const.__version__


def diff(baseline: dict[str, list[Row]], current: dict[str, list[Row]]) -> dict[str, list[str]]:
    """Rows new, gone or changed since the baseline, per threshold. Empty lists mean no difference."""
    out: dict[str, list[str]] = {}
    for tier in TIERS:
        before = {str(r["component"]): r for r in baseline.get(tier, [])}
        after = {str(r["component"]): r for r in current.get(tier, [])}
        lines = []
        for name in sorted(after.keys() - before.keys()):
            r = after[name]
            lines.append(f"NEW      {name:24} {'GATED' if gated(r) else 'ungated'}  matched {summary(r)}")
        for name in sorted(before.keys() - after.keys()):
            lines.append(f"GONE     {name:24} (was {'GATED' if gated(before[name]) else 'ungated'}, matched {summary(before[name])})")
        for name in sorted(before.keys() & after.keys()):
            b, a = before[name], after[name]
            if gated(b) != gated(a):
                lines.append(f"CHANGED  {name:24} {'GATED' if gated(b) else 'ungated'} -> {'GATED' if gated(a) else 'ungated'}")
            elif b["gate_calls"] != a["gate_calls"]:
                lines.append(f"CHANGED  {name:24} is_allowed_path calls (gate) {b['gate_calls']} -> {a['gate_calls']}")
            if b["tokens"] != a["tokens"]:
                lines.append(f"CHANGED  {name:24} matches {summary(b)} -> {summary(a)}")
        out[tier] = lines
    return out


def load_baseline() -> dict:
    return json.loads(BASELINE.read_text())


def write_baseline(current: dict[str, list[Row]]) -> None:
    previous = load_baseline() if BASELINE.exists() else {}
    data = {
        "homeassistant": installed_version(),
        "read_note": previous.get("read_note", ""),
        "tight": current["tight"],
        "loose": current["loose"],
        # Hand-kept: which flows a person has read, and what took each out. Never rewritten from a scan.
        "read": previous.get("read", {}),
    }
    BASELINE.write_text(render(data))


def render(data: dict) -> str:
    """Indented JSON with each row on one line, so a bump's diff of the baseline is a diff of rows."""
    parts = []
    for key, value in data.items():
        if key in TIERS:
            body = ",\n".join("    " + json.dumps(row) for row in value)
            parts.append(f'  {json.dumps(key)}: [\n{body}\n  ]')
        elif key == "read":
            body = ",\n".join(f"    {json.dumps(name)}: {json.dumps(entry)}" for name, entry in value.items())
            parts.append(f'  "read": {{\n{body}\n  }}')
        else:
            parts.append(f"  {json.dumps(key)}: {json.dumps(value)}")
    return "{\n" + ",\n".join(parts) + "\n}\n"


def listing(current: dict[str, list[Row]], read: dict[str, dict]) -> str:
    lines = []
    for tier, label in (("tight", "TIGHT (explicit file-path fields)"), ("loose", "LOOSE (+ any _PATH, key_file)")):
        rows = current[tier]
        ungated = [r for r in rows if not gated(r)]
        unread = [r for r in ungated if r["component"] not in read]
        lines.append(f"{label}: {len(rows)} flows, {len(rows) - len(ungated)} gated, {len(ungated)} ungated, {len(unread)} of those never read")
        for r in rows:
            mark = "GATED  " if gated(r) else ("read   " if r["component"] in read else "unread ")
            lines.append(f"  {mark} {str(r['component']):24} matched {summary(r)}")
        lines.append("")
    return "\n".join(lines)


CAVEAT = """\
'ungated' means the flow does not call is_allowed_path. It does NOT mean dangerous, and 'unread'
means nobody has looked, not that it is safe:
  - a serial/device path (`/dev/ttyUSB0`) is not a disclosure primitive
  - an HTTP route is not a filesystem path at all
  - a credential file the component consumes without echoing is a lower thing
#256 is the reading that separates those. Do not deny on this output alone.
This reads only config_flow.py: a clean diff is not "no new path fields"."""


def main(argv: list[str]) -> int:
    current = scan()
    if "--write-baseline" in argv:
        write_baseline(current)
        print(f"wrote {BASELINE.name} for homeassistant=={installed_version()}")
        return 0
    baseline = load_baseline()
    if "--list" in argv:
        print(listing(current, baseline["read"]))
        print(CAVEAT)
        return 0
    print(f"baseline: homeassistant=={baseline['homeassistant']}   installed: homeassistant=={installed_version()}")
    changes = diff(baseline, current)
    for tier in TIERS:
        print(f"\n{tier.upper()}: {len(changes[tier])} difference(s)")
        for line in changes[tier]:
            print(f"  {line}")
    new = sorted({line.split()[1] for tier in TIERS for line in changes[tier] if line.startswith(("NEW", "CHANGED"))})
    if any(changes.values()):
        print(f"\nRead before merging the bump: {', '.join(new) if new else '(only rows that went away)'}")
        print("Then record what you found in the baseline's `read` map and run --write-baseline.\n")
        print(CAVEAT)
        return 1
    print("\nno difference from the baseline.\n")
    print(CAVEAT)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
