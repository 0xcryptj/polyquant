/**
 * React chart embeds - mounts into existing dashboard.
 * Load this bundle from the vanilla index.html; it mounts TradingView + EquityChart.
 */
import { createRoot } from 'react-dom/client';
import ChartsRow from './components/ChartsRow';
import './charts-embed.css';

const root = document.getElementById('charts-root');
if (root) {
  createRoot(root).render(<ChartsRow />);
}
