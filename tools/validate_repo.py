"""Self-contained validator for the folder-per-area GitOps repo.

Asserts the invariants that promotion + naming must hold on the committed state, as a
backstop to per-step error handling. ZERO project dependencies beyond PyYAML, so the
SAME file runs in the POC (imported), the CLI, and CI (committed to the repo as
tools/validate_repo.py and run by a GitHub Action).

Checks:
  A identity & placement   obj_id ends __<area>, clean slug base, no guid, filename match
  B naming convention      area marker (end-user clean in live; admin marked everywhere),
                           connection reference points at the area's connection
  C referential integrity  every model/liveboard ref resolves in-area and is same-area
  D cross-area parity       live bases ⊆ test ⊆ config (catch partial promotions)
  E hygiene                 parseable, has name + obj_id + type body, .tml only
"""
import os
import re

import yaml

AREAS = ("config", "test", "live")
TYPE_KEYS = ("liveboard", "answer", "model", "worksheet",
             "table", "view", "sql_view", "connection", "pinboard")
ADMIN_TYPES = {"table", "view", "sql_view", "connection"}     # marked in all areas
ENDUSER_TYPES = {"model", "worksheet", "answer", "liveboard", "pinboard"}  # clean in live
_MARKER = re.compile(r"\[(config|test|live)\]\s*$", re.IGNORECASE)
_SLUG = re.compile(r"^[a-z0-9_]+$")


def _f(level, area, file, check, msg):
    return {"level": level, "area": area, "file": file, "check": check, "msg": msg}


def tml_type(doc):
    if isinstance(doc, dict):
        for k in TYPE_KEYS:
            if k in doc:
                return k
    return None


def _base_area(obj_id):
    if obj_id and "__" in obj_id:
        b, a = obj_id.rsplit("__", 1)
        return b, a
    return (obj_id or ""), None


def _refs(typ, obj):
    refs = []
    if typ in ("model", "worksheet"):
        refs.extend(obj.get("model_tables") or obj.get("tables") or [])
    if typ in ("liveboard", "answer", "pinboard"):
        for viz in obj.get("visualizations", []):
            refs.extend(viz.get("answer", {}).get("tables", []))
        refs.extend(obj.get("tables", []))
    return refs


def check_area(area, docs_by_file):
    """A, B, C, E within one area folder. docs_by_file: {filename: parsed-doc}.
    Returns (findings, bases-present)."""
    findings = []
    bases = set()

    for fn, doc in docs_by_file.items():
        if isinstance(doc, dict) and "__parse_error__" in doc:
            findings.append(_f("error", area, fn, "parse", doc["__parse_error__"]))
            continue
        typ = tml_type(doc)
        if not typ:
            findings.append(_f("error", area, fn, "type", "no recognizable TML type key"))
            continue
        obj = doc.get(typ) or {}
        oid = doc.get("obj_id", "")
        base, suf = _base_area(oid)
        if base:
            bases.add(base)

        # A · identity & placement
        if not oid:
            findings.append(_f("error", area, fn, "obj_id", "missing obj_id"))
        else:
            if suf != area:
                findings.append(_f("error", area, fn, "obj_id-area",
                                   f"obj_id '{oid}' does not end with __{area}"))
            if base and not _SLUG.match(base):
                findings.append(_f("error", area, fn, "obj_id-base",
                                   f"base '{base}' is not a clean slug [a-z0-9_]"))
        if isinstance(doc, dict) and "guid" in doc:
            findings.append(_f("error", area, fn, "guid",
                               "guid present (must be stripped; obj_id is the identity)"))

        # A · filename <base>.<type>.tml
        parts = fn.split(".")
        if len(parts) >= 3 and parts[-1] == "tml":
            f_base, f_type = ".".join(parts[:-2]), parts[-2]
            if base and f_base != base:
                findings.append(_f("warn", area, fn, "filename-base",
                                   f"filename base '{f_base}' != obj_id base '{base}'"))
            ok = (f_type == typ or {f_type, typ} <= {"model", "worksheet"}
                  or {f_type, typ} <= {"liveboard", "pinboard"})
            if not ok:
                findings.append(_f("warn", area, fn, "filename-type",
                                   f"filename type '.{f_type}.' != TML type '{typ}'"))
        else:
            findings.append(_f("warn", area, fn, "filename",
                               "filename is not <base>.<type>.tml"))

        # E · hygiene
        name = obj.get("name") if isinstance(obj, dict) else None
        if not name:
            findings.append(_f("error", area, fn, "name", "object has no name"))

        # B · name marker (audience-dependent)
        if name:
            m = _MARKER.search(name)
            marked = m.group(1).lower() if m else None
            if typ in ADMIN_TYPES:
                if marked != area:
                    findings.append(_f("error", area, fn, "name-marker",
                        f"admin object '{name}' should end with [{area.capitalize()}]"))
            elif area == "live":
                if marked:
                    findings.append(_f("error", area, fn, "name-marker",
                        f"live end-user object '{name}' must NOT carry an area marker"))
            elif marked != area:
                findings.append(_f("error", area, fn, "name-marker",
                    f"end-user object '{name}' should end with [{area.capitalize()}]"))

        # B · connection reference points at this area
        conns = []
        if typ in ("table", "view", "sql_view"):
            c = obj.get("connection")
            if isinstance(c, dict) and c.get("name"):
                conns.append(c["name"])
        if typ in ("model", "worksheet"):
            for r in _refs(typ, obj):
                c = r.get("connection")
                if isinstance(c, dict) and c.get("name"):
                    conns.append(c["name"])
        for cn in conns:
            low = cn.lower()
            if area not in low:
                findings.append(_f("error", area, fn, "connection",
                    f"connection '{cn}' does not reference the {area} area"))
            else:
                others = [a for a in AREAS if a != area and a in low]
                if others:
                    findings.append(_f("error", area, fn, "connection",
                        f"connection '{cn}' references another area ({', '.join(others)})"))

    # C · referential integrity (intra-area)
    for fn, doc in docs_by_file.items():
        typ = tml_type(doc)
        if not typ:
            continue
        obj = doc.get(typ) or {}
        for r in _refs(typ, obj):
            roid = r.get("obj_id")
            if not roid:
                findings.append(_f("warn", area, fn, "ref-obj_id",
                    f"reference {r.get('name')!r} has no obj_id (cannot verify)"))
                continue
            rbase, rsuf = _base_area(roid)
            if rsuf != area:
                findings.append(_f("error", area, fn, "ref-area",
                    f"reference '{roid}' is not in the {area} area"))
            elif rbase not in bases:
                findings.append(_f("error", area, fn, "ref-missing",
                    f"reference base '{rbase}' has no object in {area}/"))

    return findings, bases


def check_all(docs_by_area):
    """All checks across the three folders. docs_by_area: {area: {file: doc}}."""
    findings, base_sets = [], {}
    for area in AREAS:
        f, bases = check_area(area, docs_by_area.get(area, {}))
        findings += f
        base_sets[area] = bases
    # D · parity: each area's bases must exist in the one upstream of it
    for downstream, upstream in (("live", "test"), ("test", "config")):
        for b in sorted(base_sets.get(downstream, set()) - base_sets.get(upstream, set())):
            findings.append(_f("warn", downstream, "-", "parity",
                f"base '{b}' is in {downstream}/ but missing from {upstream}/ (partial promotion?)"))
    return findings


def load_local(root="."):
    """{area: {file: doc}} from local config/ test/ live/ dirs (for CI)."""
    out = {}
    for area in AREAS:
        d, files = os.path.join(root, area), {}
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".tml"):
                    continue
                try:
                    with open(os.path.join(d, fn)) as fh:
                        files[fn] = yaml.safe_load(fh)
                except Exception as e:                       # noqa: BLE001
                    files[fn] = {"__parse_error__": str(e)}
        out[area] = files
    return out


def summarize(findings):
    return (sum(1 for x in findings if x["level"] == "error"),
            sum(1 for x in findings if x["level"] == "warn"))


if __name__ == "__main__":   # CI entry point (tools/validate_repo.py)
    import sys
    findings = check_all(load_local("."))
    for x in findings:
        print(f"[{x['level'].upper():5}] {x['area']}/{x['file']:30} {x['check']:14} {x['msg']}")
    errs, warns = summarize(findings)
    print(f"\n{errs} error(s), {warns} warning(s)")
    sys.exit(1 if errs else 0)
