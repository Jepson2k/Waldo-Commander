"""Run one calibration routine against Commander's managed par6d.

Open this file in Commander and set ROUTINE (or PAR6_CALIBRATION) to check,
tune-feedback, gravity, verify-gravity or limits. The runtime must have been
started with PAR6_DIAGNOSTICS pointing at its native recording. Results and
the staged candidate/rollback configs land under calibration-runs/.
"""

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from par6 import AsyncRobotClient
from par6.calibration import Session, check, gravity, limits, tune_feedback

ROUTINE = os.environ.get("PAR6_CALIBRATION", "check")
JOINT = int(os.environ.get("PAR6_CALIBRATION_JOINT", "3"))  # tune-feedback, 1-6
ROOT = Path.cwd() / "calibration-runs"
CAPTURE = Path(os.environ.get("PAR6_DIAGNOSTICS", ROOT / "capture.bin"))


async def main():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    client = AsyncRobotClient(host="127.0.0.1", port=5001)
    try:
        async with Session(client, ROOT / f"{stamp}-{ROUTINE}", CAPTURE) as session:
            if ROUTINE == "check":
                report = await check(session)
            elif ROUTINE == "tune-feedback":
                report = await tune_feedback(session, joint=JOINT - 1)
            elif ROUTINE == "gravity":
                report = await gravity(session)
            elif ROUTINE == "verify-gravity":
                report = await gravity(session, verify_only=True)
            elif ROUTINE == "limits":
                report = await limits(session)
            else:
                raise SystemExit(f"Unknown routine {ROUTINE!r}")
            print(
                f"{report['kind']}: {'PASS' if report['valid'] else 'FAIL'}",
                report["reasons"],
                "evidence:",
                session.directory,
                flush=True,
            )
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
