import React, { useEffect, useRef, memo } from 'react';

function TradingViewWidget() {
  const container = useRef();

  useEffect(() => {
    const script = document.createElement('script');
    script.src = 'https://s3.tradingview.com/external-embedding/embed-widget-symbol-overview.js';
    script.type = 'text/javascript';
    script.async = true;
    script.innerHTML = JSON.stringify({
      lineWidth: 2,
      lineType: 0,
      chartType: 'area',
      fontColor: 'rgb(106, 109, 120)',
      gridLineColor: 'rgba(242, 242, 242, 0.06)',
      volumeUpColor: 'rgba(34, 197, 94, 0.5)',
      volumeDownColor: 'rgba(239, 68, 68, 0.5)',
      backgroundColor: '#070b11',
      widgetFontColor: '#dde4ee',
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderUpColor: '#22c55e',
      borderDownColor: '#ef4444',
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
      colorTheme: 'dark',
      isTransparent: false,
      locale: 'en',
      chartOnly: false,
      scalePosition: 'right',
      scaleMode: 'Normal',
      fontFamily: "'JetBrains Mono', monospace",
      valuesTracking: '1',
      changeMode: 'price-and-percent',
      symbols: [
        ['Bitcoin', 'BINANCE:BTCUSDT|1D'],
        ['Ethereum', 'BINANCE:ETHUSDT|1D'],
        ['Solana', 'BINANCE:SOLUSDT|1D'],
      ],
      dateRanges: ['1d|1', '1m|30', '3m|60', '12m|1D', '60m|1W', 'all|1M'],
      fontSize: '10',
      headerFontSize: 'medium',
      autosize: true,
      width: '100%',
      height: '100%',
      noTimeScale: false,
      hideDateRanges: false,
      hideMarketStatus: false,
      hideSymbolLogo: false,
    });
    container.current.appendChild(script);
    return () => {
      if (container.current) {
        const child = container.current.querySelector('script');
        if (child) child.remove();
      }
    };
  }, []);

  return (
    <div className="tradingview-widget-container" ref={container} style={{ width: '100%', height: '100%', minHeight: 220, flex: 1 }}>
      <div className="tradingview-widget-container__widget" />
      <div className="tradingview-widget-copyright">
        <a href="https://www.tradingview.com/symbols/BTCUSDT/" rel="noopener noreferrer" target="_blank">
          <span className="blue-text">BTC</span>
        </a>
        <span className="comma">, </span>
        <a href="https://www.tradingview.com/symbols/ETHUSDT/" rel="noopener noreferrer" target="_blank">
          <span className="blue-text">ETH</span>
        </a>
        <span className="comma">, </span>
        <a href="https://www.tradingview.com/symbols/SOLUSDT/" rel="noopener noreferrer" target="_blank">
          <span className="blue-text">SOL</span>
        </a>
        <span className="trademark"> by TradingView</span>
      </div>
    </div>
  );
}

export default memo(TradingViewWidget);
