// Both clocks contribute so a short peak cannot disappear between slower samples.
// A long record is sent thinned, so two neighbouring rows can be several of
// its samples apart: intervals that close a gap that wide are one.
const spacing = (r) => r.dt * Math.ceil((r.source[r.rows - 1] + 1) / Math.max(1, r.rows));

export function deviations(records, data, thresholds) {
  const mask = new Uint8Array(records[1].rows), raw = [];
  const period = Math.min(spacing(records[0]), spacing(records[1]));
  let peak = null;
  for (const record of [1, 0]) {
    const r = records[record], errors = record ? data : data.idealSamples;
    for (let block = 0; block < r.blocks.length; block++) {
      const b = r.blocks[block];
      if (b.instant || !b.valid) continue;
      let interval = null;
      for (let row = b.start; row < b.end; row++) {
        const values = [errors.positionMm[row], errors.orientationDeg[row], errors.jointDeg[row]];
        const score = values.every(v => v !== null && Number.isFinite(v))
          ? Math.max(...values.map((v, i) => v / thresholds[i])) : 0;
        if (score <= 1) { interval = null; continue; }
        const predictedRow = record ? row : errors.predictedRows[row];
        const idealRow = record ? data.idealRows[row] : row;
        if (predictedRow < 0 || idealRow < 0) { interval = null; continue; }
        mask[predictedRow] = 1;
        const elapsed = (r.source[row] - r.source[b.start]) * r.dt;
        const hit = { record, row, block, idealRow, score, elapsed };
        if (!peak || score > peak.score) peak = hit;
        if (!interval) {
          interval = { key: `${b.command}/${b.commandOccurrence}`, first: idealRow, last: idealRow, timeStart: elapsed, timeEnd: elapsed, peak: hit };
          raw.push(interval);
        }
        interval.last = idealRow;
        interval.timeEnd = elapsed;
        if (score > interval.peak.score) interval.peak = hit;
      }
    }
  }
  raw.sort((a, b) => a.first - b.first || a.timeStart - b.timeStart);
  const intervals = [];
  for (const next of raw) {
    const prior = intervals.at(-1);
    if (prior && prior.key === next.key && next.timeStart <= prior.timeEnd + period + 1e-9) {
      prior.last = Math.max(prior.last, next.last);
      prior.timeEnd = Math.max(prior.timeEnd, next.timeEnd);
      if (next.peak.score > prior.peak.score) prior.peak = next.peak;
    } else intervals.push(next);
  }
  return { mask, intervals, peak };
}
