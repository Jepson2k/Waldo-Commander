// Appends a stream's new samples to a live ECharts chart and keeps its newest
// `keep` points per series, so a push carries only what arrived since the last.
// Each row is [time, value for series 0, value for series 1, ...]; a null value
// adds nothing to that series.
window.liveChartAppend = function (id, rows, keep) {
  const el = getElement(id);
  const chart = el && el.chart;
  if (!chart) return;
  // The element re-renders from fresh options on a remount or a server update;
  // the buffers start over from those.
  if (!el.liveSeries || el.liveFrom !== el.options) {
    el.liveSeries = (el.options.series || []).map((s) => (s.data || []).slice());
    el.liveFrom = el.options;
  }
  const series = el.liveSeries;
  for (const row of rows) {
    for (let s = 0; s < series.length && s + 1 < row.length; s++) {
      if (row[s + 1] !== null) series[s].push([row[0], row[s + 1]]);
    }
  }
  for (const data of series) {
    if (data.length > keep) data.splice(0, data.length - keep);
  }
  // Lazy: pushes that land between frames are drawn once.
  chart.setOption({ series: series.map((data) => ({ data })) }, { lazyUpdate: true });
};

// Replaces a live chart's buffers with the whole of each series, for a chart
// that missed pushes while it was covered.
window.liveChartLoad = function (id, series) {
  const el = getElement(id);
  const chart = el && el.chart;
  if (!chart) return;
  el.liveSeries = series;
  el.liveFrom = el.options;
  chart.setOption({ series: series.map((data) => ({ data })) }, { lazyUpdate: true });
};
