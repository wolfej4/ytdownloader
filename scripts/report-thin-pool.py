#!/usr/bin/env python3
"""Export thin-pool capacity from the Proxmox node to an existing shared mount."""

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


def report(pool, output):
    status = {"checked_at": time.time(), "pool": pool}
    try:
        result = subprocess.run(
            ["lvs", "--reportformat", "json", "--units", "b", "--nosuffix", "-o",
             "lv_name,vg_name,data_percent,metadata_percent,lv_attr", pool],
            check=True, capture_output=True, text=True, timeout=15,
        )
        volume = json.loads(result.stdout)["report"][0]["lv"][0]
        if not volume["lv_attr"].startswith("t"):
            raise ValueError(f"{pool} is not an active thin pool")
        status.update(data_percent=float(volume["data_percent"]), metadata_percent=float(volume["metadata_percent"]), error="")
    except (subprocess.SubprocessError, OSError, KeyError, ValueError, IndexError) as exc:
        status["error"] = str(exc)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=output.parent, delete=False, encoding="utf-8") as fh:
        temporary = Path(fh.name)
        json.dump(status, fh)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        temporary.chmod(0o644)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return status


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", default="pve/data")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(report(arguments.pool, arguments.output)))
