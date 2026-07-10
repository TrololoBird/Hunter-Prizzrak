import pathlib

HUNT_CORE = pathlib.Path("hunt_core")

def classify(f: pathlib.Path) -> str:
    rel = f.relative_to(HUNT_CORE)
    parts = rel.parts
    name = f.name

    # diagnostics
    if "diagnostic" in name or parts[0] == "diagnostics":
        return "diagnostics"

    # startup
    if name in ("bootstrap.py", "paths.py", "_cli.py", "__main__.py", "secrets.py", "data_readiness.py"):
        return "startup"
    if rel == pathlib.Path("domain/config.py"):
        return "startup"

    # contract / type
    if "contract" in name:
        return "contract-type"
    if name == "errors.py":
        return "contract-type"
    if name == "types.py":
        return "contract-type"
    if name in ("schemas.py", "signal_horizon.py", "snapshot.py", "structure_state.py"):
        return "contract-type"
    if name == "setup_fields.py":
        return "contract-type"
    if name == "model.py":
        return "contract-type"

    # runtime directories (hot path per-tick)
    runtime_dirs = {
        "runtime", "market", "features", "maps",
        "scanner/detect", "scanner/gate",
        "signals", "track", "deliver",
        "expansion", "analysis", "regime",
        "analyst", "confluence", "levels",
        "data", "params",
    }
    # scanner top-level files + scanner/setups, scanner/telegram
    scanner_runtime = {"prescan.py", "telegram.py"}
    if name in scanner_runtime and parts[0] == "scanner":
        return "runtime"
    if len(parts) >= 2:
        key = str(parts[0])
        if len(parts) >= 2 and parts[0] in ("scanner", "analyst", "expansion"):
            key = f"{parts[0]}/{parts[1]}"
        if key in runtime_dirs:
            return "runtime"
        # sub-dirs of runtime dirs
        if parts[0] in {"runtime", "market", "features", "maps", "analysis", "signals", "track", "deliver", "regime", "levels", "confluence", "data", "params"}:
            return "runtime"
        if parts[0] == "scanner" and parts[1] in {"detect", "gate"}:
            return "runtime"
        if parts[0] == "expansion" and parts[1] in {"blocks", "execution", "forecast", "learning", "ranking", "rotation"}:
            return "runtime"
        if parts[0] == "analyst" and parts[1] in {"verdict_v2"}:
            return "runtime"
        if parts[0] == "scanner" and parts[1] in {"setups"}:
            return "runtime"

    # everything under scanner/ except prescan.py/telegram.py (those are matched above or as top-level)
    if parts[0] == "scanner" and parts[1] in {"detect", "gate", "setups"}:
        return "runtime"

    if parts[0] == "analyst":
        return "runtime"

    # expansion subdirs
    if parts[0] == "expansion":
        return "runtime"

    # clock
    if name == "clock.py":
        return "runtime"

    # domain/ files not covered
    if parts[0] == "domain" and name not in ("config.py",):
        return "other"

    return "other"


def main():
    categories = {}
    for f in sorted(HUNT_CORE.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        if f.name == "__init__.py":
            continue
        cat = classify(f)
        categories.setdefault(cat, []).append(f)
    for cat in ("runtime", "startup", "diagnostics", "contract-type", "other"):
        files = categories.get(cat, [])
        print(f"\n{'='*60}")
        print(f"{cat.upper():^60}")
        print(f"{'='*60}")
        print(f"Count: {len(files)}")
        for f in files:
            rel = f.relative_to(HUNT_CORE)
            print(f"  {rel}")


if __name__ == "__main__":
    main()
