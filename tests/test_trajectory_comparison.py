"""Full-resolution comparisons preserve timing, orientation and missing data."""

from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest
from waldoctl import TickBlock, TickIndex

from waldo_commander.services.trajectory_comparison import comparison_samples


def record(rows, blocks=None, dt=0.02):
    return TickIndex(
        row_dt_s=dt,
        joints_rad=np.zeros((rows, 6)),
        tcp=np.zeros((rows, 6)),
        tool_closed=np.zeros(rows),
        tool_gripping=np.zeros(rows, dtype=bool),
        blocks=blocks or (TickBlock(command=0, start_row=0, rows=rows),),
    )


def test_comparison_preserves_short_peaks_and_orientation_only_and_joint_wrap():
    ideal, predicted = record(30_000), record(30_000)
    predicted.tcp[17_001, 0] = 0.0015
    predicted.tcp[19_011, 5] = np.radians(0.7)
    ideal.joints_rad[20_021, 0] = np.radians(179)
    predicted.joints_rad[20_021, 0] = np.radians(-179)
    result = comparison_samples(ideal, predicted, [True, *[False] * 5])
    assert np.argmax(result["positionMm"]) == 17_001
    assert result["positionMm"][17_001] == pytest.approx(1.5)
    assert result["orientationDeg"][19_011] == pytest.approx(0.7)
    assert result["positionMm"][19_011] == 0
    assert result["jointDeg"][20_021] == pytest.approx(2)
    bounded = comparison_samples(ideal, predicted, [False] * 6)
    assert bounded["jointDeg"][20_021] == pytest.approx(358)
    matching = comparison_samples(ideal, ideal, [False] * 6)
    assert not any(matching["positionMm"])
    assert not any(matching["orientationDeg"])
    assert not any(matching["jointDeg"])


def test_alignment_uses_command_occurrence_and_elapsed_time_without_duration_scaling():
    ideal = record(8, (TickBlock(4, 0, 4), TickBlock(4, 4, 4)))
    predicted = record(
        12, (TickBlock(4, 0, 6), TickBlock(4, 6, 4), TickBlock(9, 10, 2))
    )
    ideal.tcp[:, 0] = np.arange(8) * 0.001
    predicted.tcp[:6, 0] = [0, 0, 0.001, 0.002, 0.003, 0.003]
    predicted.tcp[6:10, 0] = ideal.tcp[4:, 0]
    result = comparison_samples(ideal, predicted, [False] * 6)
    assert result["idealRows"] == [0, 1, 2, 3, 3, 3, 4, 5, 6, 7, -1, -1]
    assert result["positionMm"][:6] == pytest.approx([0, 1, 1, 1, 0, 0])
    assert result["positionMm"][10:] == [None, None]
    assert result["settling"][:6] == [False] * 4 + [True] * 2
    partial = comparison_samples(
        replace(ideal, stop="budget_exhausted"), predicted, [False] * 6
    )
    assert partial["idealRows"][4:6] == [-1, -1]
    fast = comparison_samples(ideal, replace(predicted, row_dt_s=0.01), [False] * 6)
    assert fast["idealRows"][6:10] == [4, 5, 5, 6]
    predicted.valid = np.ones(12, dtype=bool)
    predicted.valid[7] = False
    predicted.tcp[8, 2] = np.nan
    invalid = comparison_samples(ideal, predicted, [False] * 6)
    assert invalid["idealRows"][7:9] == [-1, -1]
    assert invalid["positionMm"][7:9] == [None, None]


def test_ideal_inspection_pairs_every_sample_within_partial_prediction_coverage():
    ideal, predicted = record(8, dt=0.02), record(3, dt=0.04)
    predicted.stop = "budget_exhausted"
    ideal.tcp[:, 0] = np.arange(8) * 0.001
    predicted.tcp[:, 0] = [0, 0.002, 0.004]
    result = comparison_samples(ideal, predicted, [False] * 6)
    pairs = result["idealSamples"]
    assert pairs["predictedRows"] == [0, 1, 1, 2, 2, -1, -1, -1]
    assert pairs["positionMm"][:5] == pytest.approx([0, 1, 0, 1, 0])
    assert pairs["positionMm"][5:] == [None] * 3


def _browser_deviations(ideal, predicted):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the browser's interval algorithm")
    data = comparison_samples(ideal, predicted, [False] * 6)
    records = [
        {
            "rows": r.rows,
            "dt": r.row_dt_s,
            "source": list(range(r.rows)),
            "blocks": [
                {
                    "start": 0,
                    "end": r.rows,
                    "valid": True,
                    "command": 0,
                    "commandOccurrence": 0,
                }
            ],
        }
        for r in (ideal, predicted)
    ]
    module = Path("waldo_commander/scene3d/js/deviations.js").resolve()
    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            """
      import {readFileSync} from 'node:fs';
      const {deviations} = await import('data:text/javascript;base64,'+readFileSync(process.argv[1]).toString('base64'));
      const {records,data} = JSON.parse(readFileSync(0,'utf8'));
      const result=deviations(records,data,[1,.5,.5]);
      console.log(JSON.stringify({...result,mask:Array.from(result.mask)}));
    """,
            str(module),
        ],
        input=json.dumps({"records": records, "data": data}),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_display_intervals_and_peak_include_a_short_ideal_side_deviation():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to exercise the browser's interval algorithm")
    ideal, predicted = record(7, dt=0.02), record(4, dt=0.04)
    ideal.tcp[1, 5] = np.radians(0.7)
    ideal.tcp[3, 0] = 0.0015
    data = comparison_samples(ideal, predicted, [False] * 6)
    assert not any(data["positionMm"]) and not any(data["orientationDeg"])
    result = _browser_deviations(ideal, predicted)
    assert result["peak"]["record"] == 0
    assert result["peak"]["row"] == 3
    assert [(i["first"], i["last"]) for i in result["intervals"]] == [(1, 1), (3, 3)]
    assert result["mask"] == [0, 1, 1, 0]

    ideal, predicted = record(3), record(8)
    predicted.tcp[3, 0] = 0.002
    predicted.tcp[6, 0] = 0.003
    settling = _browser_deviations(ideal, predicted)
    assert [i["peak"]["row"] for i in settling["intervals"]] == [3, 6]
    assert settling["peak"]["row"] == 6
