import React from 'react';
import TradingViewWidget from './TradingViewWidget';
import EquityChart from './EquityChart';

export default function ChartsRow() {
  return (
    <div className="charts-row" style={{ flex: '1 1 auto', minHeight: 220, display: 'flex', gap: 2, height: '100%' }}>
      <div className="panel chart-panel btc-panel" style={{ flex: 1.5, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <div className="ph" style={{ flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0 7px', height: 22, borderBottom: '1px solid var(--border)' }}>
          <span className="ph-title" style={{ fontSize: 8, fontWeight: 700, color: 'var(--muted)', textTransform: 'uppercase' }}>BTC / ETH / SOL</span>
        </div>
        <div className="chart-body" style={{ flex: 1, minHeight: 0, padding: '2px 4px' }}>
          <TradingViewWidget />
        </div>
      </div>
      <div className="panel chart-panel pnl-panel" style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <div className="ph" style={{ flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0 7px', height: 22, borderBottom: '1px solid var(--border)' }}>
          <span className="ph-title" style={{ fontSize: 8, fontWeight: 700, color: 'var(--muted)', textTransform: 'uppercase' }}>EQUITY</span>
        </div>
        <div className="chart-body" style={{ flex: 1, minHeight: 0, padding: '2px 4px' }}>
          <EquityChart />
        </div>
      </div>
    </div>
  );
}
