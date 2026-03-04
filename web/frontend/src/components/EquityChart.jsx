import React, { useEffect, useState } from 'react';
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from 'chart.js';
import { Line } from 'react-chartjs-2';

ChartJS.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler
);

const CHART_GREEN = '#22c55e';
const CHART_RED = '#ef4444';

const chartOpts = {
  responsive: true,
  maintainAspectRatio: false,
  animation: { duration: 300 },
  interaction: { intersect: false, mode: 'index' },
  plugins: {
    legend: { display: false },
    tooltip: {
      backgroundColor: 'rgba(7,11,17,.97)',
      borderColor: '#1a2433',
      padding: 6,
      callbacks: {
        label: (c) => `Equity: $${Number(c.raw).toFixed(2)}`,
      },
    },
  },
  scales: {
    x: {
      grid: { color: 'rgba(21,30,44,.6)' },
      ticks: { maxTicksLimit: 8, font: { size: 8 }, color: '#36404e' },
    },
    y: {
      grid: { color: 'rgba(21,30,44,.6)' },
      ticks: {
        callback: (v) => `$${Number(v).toFixed(0)}`,
        font: { size: 8 },
        color: '#36404e',
      },
      beginAtZero: false,
    },
  },
};

async function fetchPnlHistory() {
  const r = await fetch('/api/pnl-history');
  if (!r.ok) throw new Error(r.statusText);
  return r.json();
}

function EquityChart() {
  const [data, setData] = useState(null);
  const [badge, setBadge] = useState('—');

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const series = await fetchPnlHistory();
        if (cancelled || !series?.length) return;
        const starting = series[0]?.cumulative ?? 1000;
        const labels = series.map((_, i) => (i === 0 ? 'Start' : `#${i}`));
        const values = series.map((s) => s.cumulative);
        const last = values[values.length - 1];
        const diff = last - starting;
        const up = diff >= 0;
        setBadge(`${diff >= 0 ? '+' : '-'}$${Math.abs(diff).toFixed(2)}`);
        setData({
          labels,
          datasets: [
            {
              label: 'Equity',
              data: values,
              borderColor: up ? CHART_GREEN : CHART_RED,
              backgroundColor: up ? 'rgba(34,197,94,0.15)' : 'rgba(239,68,68,0.12)',
              fill: true,
              tension: 0.3,
              borderWidth: 2,
              pointRadius: 0,
            },
            {
              label: 'Baseline',
              data: values.map(() => starting),
              borderColor: 'rgba(86,96,112,.45)',
              borderDash: [3, 3],
              borderWidth: 1,
              pointRadius: 0,
              fill: false,
            },
          ],
        });
      } catch (e) {
        console.error('EquityChart:', e);
      }
    };
    load();
    const t = setInterval(load, 5000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  if (!data) return <div className="chart-loading">Loading equity…</div>;

  return (
    <div className="equity-chart-wrap" style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      <div className="chart-badge" style={{ color: data.datasets[0].borderColor }}>{badge}</div>
      <div style={{ flex: 1, minHeight: 180 }}>
        <Line data={data} options={chartOpts} />
      </div>
    </div>
  );
}

export default EquityChart;
