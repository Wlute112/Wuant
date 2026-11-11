import { useEffect, useRef, useState } from 'react';
import ModelDecisionTape from '../model-tape/ModelDecisionTape.jsx';
import { CHART_DEFAULTS, CHART_PRESETS, readChartSettings, sanitizeSettings } from '../../lib/chartView.js';
import './live-chart.css';

export default function LiveChart({ mode, barHours, onStrategyInterval, marketControls, ...props }) {
  const key = `quant.chart.v1.${mode}.${props.assetClass}`;
  const [settings, setSettings] = useState(() => readChartSettings(key));
  const [presets, setPresets] = useState(() => { try { return JSON.parse(localStorage.getItem(`${key}.presets`)) || {}; } catch { return {}; } });
  const [panel, setPanel] = useState(null);
  const [tab, setTab] = useState('Appearance');
  const [name, setName] = useState('');
  const [expanded, setExpanded] = useState(false);
  const root = useRef(null);
  const trigger = useRef(null);
  useEffect(() => { try { localStorage.setItem(key, JSON.stringify(settings)); localStorage.setItem(`${key}.presets`, JSON.stringify(presets)); } catch { /* Browser storage may be disabled. */ } }, [key, settings, presets]);
  useEffect(() => {
    if (!panel) return;
    const dialog = root.current.querySelector('[role="dialog"]');
    dialog?.querySelector('button, input, select')?.focus();
    const handleKey = event => {
      if (event.key === 'Escape') { setPanel(null); trigger.current?.focus(); }
      if (event.key === 'Tab') {
        const elements = [...dialog.querySelectorAll('button, input, select')].filter(el => !el.disabled);
        const first = elements[0], last = elements.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    };
    dialog.addEventListener('keydown', handleKey);
    return () => dialog.removeEventListener('keydown', handleKey);
  }, [panel]);
  const update = (field, value) => setSettings(s => ({ ...s, [field]: value }));
  const layerNames = { signals: 'Model signals', levels: 'Entry / stop / target', forecasts: 'Forecast projection', regime: 'Regime probabilities', context: 'HMM fit context' };
  const unavailable = field => ['signals', 'forecasts', 'regime', 'context'].includes(field) && !props.modelOverlayActive;
  function togglePanel(value, event) { trigger.current = event.currentTarget; setPanel(panel === value ? null : value); }
  const toolbar = <> {marketControls} <div className="live-chart__toolbar">
      <label>Chart <select aria-label="Chart type" value={settings.type} onChange={e => update('type', e.target.value)}><option value="candles">Candles</option><option value="line">Line</option></select></label>
      <label>Preset <select aria-label="Chart preset" value="" onChange={e => setSettings(sanitizeSettings(CHART_PRESETS[e.target.value] || presets[e.target.value]))}><option value="" disabled>Choose preset</option>{[...Object.keys(CHART_PRESETS), ...Object.keys(presets)].map(n => <option key={n}>{n}</option>)}</select></label>
      <button aria-expanded={panel === 'Layers'} onClick={e => togglePanel('Layers', e)}>Layers</button>
      <button aria-expanded={panel === 'Settings'} onClick={e => togglePanel('Settings', e)}>Settings</button>
      <button aria-pressed={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? 'Collapse chart' : 'Expand chart'}</button>
    </div></>;
  return <section ref={root} className={`live-chart ${expanded ? 'is-expanded' : ''}`} aria-label="Chart workspace" onKeyDown={event => { if (event.key === 'Escape' && !panel) setExpanded(false); }}>

    {!props.modelOverlayActive && !props.marketOnly && <div className="live-chart__notice">Strategy overlays need {props.modelBarHours === 24 ? '1D' : `${props.modelBarHours}H`} bars. <button onClick={onStrategyInterval}>Use strategy interval</button></div>}
    <ModelDecisionTape {...props} live toolbar={toolbar} chartSettings={settings} barHours={barHours} />
    <div className="live-chart__status"><span>{props.mock ? 'Demonstration data · no broker connection' : props.marketSource}</span><span>{barHours === 24 ? '1D' : `${barHours}H`} · {props.marketUpdateMode === 'historical_snapshot' ? 'Historical snapshot · 5 min refresh' : 'Updates every 2s'} · {props.marketStatus || props.feedStatus}{props.marketAsOf ? ` · received ${new Date(props.marketAsOf).toLocaleString()}` : ''}</span><span>Model as of {props.modelAsOf ? new Date(props.modelAsOf).toLocaleString() : 'unavailable'}</span></div>
    {panel && <div className="live-chart__drawer" role="dialog" aria-modal="true" aria-label={`${panel} chart`}>
      <header><h3>{panel}</h3><button onClick={() => { setPanel(null); trigger.current?.focus(); }}>Close</button></header>
      {panel === 'Settings' && <div className="live-chart__tabs">{['Appearance', 'Scales', 'Strategy layers'].map(t => <button key={t} aria-pressed={tab === t} onClick={() => setTab(t)}>{t}</button>)}</div>}
      {(panel === 'Layers' || tab === 'Strategy layers') ? Object.entries(layerNames).map(([field, label]) => <label className="live-chart__option" key={field}><input type="checkbox" checked={settings[field]} disabled={unavailable(field)} onChange={e => update(field, e.target.checked)} />{label}{unavailable(field) && <small>Unavailable on this symbol / interval</small>}</label>) : tab === 'Appearance' ? <>
        {['grid', 'crosshair'].map(field => <label className="live-chart__option" key={field}><input type="checkbox" checked={settings[field]} onChange={e => update(field, e.target.checked)} />Show {field}</label>)}
        <label className="live-chart__option">Timezone<select value={settings.timezone} onChange={e => update('timezone', e.target.value)}><option value="local">Local</option><option value="UTC">UTC</option><option value="America/New_York">New York</option></select></label>
      </> : <><label className="live-chart__option"><input type="checkbox" checked={settings.logarithmic} disabled={props.points.some(p => [p.low, p.close].some(v => v != null && v <= 0))} onChange={e => update('logarithmic', e.target.checked)} />Logarithmic price scale</label><p>Prices autoscale to visible bars. Drag the price axis to adjust; double-click to restore autoscale. Log scale requires positive prices.</p></>}
      <p>Display settings only. Trading parameters remain controlled by the strategy.</p>
      <form onSubmit={e => { e.preventDefault(); const n = name.trim(); if (n && !Object.hasOwn(CHART_PRESETS, n)) { setPresets(p => ({ ...p, [n]: settings })); setName(''); } }}><label>Preset name<input maxLength={40} value={name} onChange={e => setName(e.target.value)} /></label><button disabled={!name.trim() || Object.hasOwn(CHART_PRESETS, name.trim())}>Save preset</button></form>
      {Object.keys(presets).map(n => <div className="live-chart__option" key={n}>{n}<button onClick={() => setPresets(p => Object.fromEntries(Object.entries(p).filter(([k]) => k !== n)))}>Delete {n}</button></div>)}
      <button onClick={() => setSettings({ ...CHART_DEFAULTS })}>Reset defaults</button>
    </div>}
  </section>;
}
