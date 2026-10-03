"""Throwaway diagnostic: how this runner delivers status and timers.

Never merged. Fails on purpose so the report lands in the CI log.
"""

import os
import statistics
import sys
import time

from parol6.client.async_client import AsyncRobotClient

from tests.conftest import _get_test_ports


def _ms(values: list[float]) -> str:
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return (
        f"n={len(values)} min={ordered[0] * 1e3:.1f} med={statistics.median(values) * 1e3:.1f}"
        f" p95={p95 * 1e3:.1f} max={ordered[-1] * 1e3:.1f}"
    )


async def test_report_status_and_timer_delivery(session_controller, capsys) -> None:
    port, _ = _get_test_ports()
    async with AsyncRobotClient(host="127.0.0.1", port=port, timeout=5.0) as client:
        rate = await client.status_rate()
        arrivals: list[float] = []
        deadline = time.monotonic() + 3.0
        async for _status in client.stream_status():
            arrivals.append(time.monotonic())
            if arrivals[-1] >= deadline:
                break
        pings: list[float] = []
        for _ in range(20):
            started = time.monotonic()
            await client.ping()
            pings.append(time.monotonic() - started)
        loop = await client.loop_stats()

    sleeps: list[float] = []
    for _ in range(50):
        started = time.monotonic()
        time.sleep(0.01)
        sleeps.append(time.monotonic() - started)

    qos = "n/a"
    qos_sleeps: list[float] = []
    if sys.platform == "darwin":
        import ctypes

        libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        qos = libsystem.pthread_set_qos_class_self_np(0x21, 0)  # USER_INTERACTIVE
        for _ in range(50):
            started = time.monotonic()
            time.sleep(0.01)
            qos_sleeps.append(time.monotonic() - started)

    # A thread that never sleeps still loses the CPU when the box is short
    # of cores: the longest stall between two clock reads shows it.
    stalls: list[float] = []
    last = time.perf_counter()
    end = last + 1.0
    while last < end:
        now = time.perf_counter()
        if now - last > 0.002:
            stalls.append(now - last)
        last = now
    import subprocess

    top = subprocess.run(
        ["ps", "-Ao", "pcpu,comm"], capture_output=True, text=True, check=False
    ).stdout.splitlines()
    busiest = sorted(top[1:], key=lambda r: -float(r.split()[0] or 0))[:6]

    gaps = [b - a for a, b in zip(arrivals, arrivals[1:])]
    report = (
        "TIMING-PROBE\n"
        f"  cpus={os.cpu_count()} loadavg={os.getloadavg()}\n"
        f"  busy-loop stalls >2 ms in 1 s: {len(stalls)} "
        f"{_ms(stalls) if stalls else ''}\n"
        f"  busiest: {busiest}\n"
        f"  status_rate={rate}\n"
        f"  frames in 3 s: {len(arrivals)}; inter-arrival ms {_ms(gaps)}\n"
        f"  ping ms {_ms(pings)}\n"
        f"  sleep(10 ms) actual ms {_ms(sleeps)}\n"
        f"  after USER_INTERACTIVE QoS (rc={qos}): sleep(10 ms) actual ms "
        f"{_ms(qos_sleeps) if qos_sleeps else 'n/a'}\n"
        f"  controller loop_stats={loop}"
    )
    # End the job here so its log is readable without waiting for the suite.
    with capsys.disabled():
        print(report, flush=True)
    os._exit(3)
