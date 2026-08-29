---
name: Strip Recorder
description: An analog seismograph/strip-chart instrument world for the trading system's reporting dashboard.
colors:
  bg-void: "#0a0b0d"
  bg-panel: "#121418"
  bg-panel-raised: "#1a1d23"
  hairline: "#2a2e37"
  paper: "#16181d"
  trace-amber: "#ffb020"
  trace-cyan: "#4fd8e0"
  trace-violet: "#8b7cf6"
  text-primary: "#e8e6df"
  text-dim: "#828998"
  threshold-warn: "#d9a441"
  threshold-danger: "#e0483f"
  positive: "#35c98d"
  negative: "#e0483f"
typography:
  display:
    fontFamily: "IBM Plex Mono, ui-monospace, SFMono-Regular, monospace"
    fontSize: "clamp(1.25rem, 1rem + 1vw, 1.75rem)"
    fontWeight: 600
    lineHeight: 1.1
    letterSpacing: "0.02em"
  label:
    fontFamily: "IBM Plex Mono, ui-monospace, SFMono-Regular, monospace"
    fontSize: "0.6875rem"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "0.12em"
  metric:
    fontFamily: "IBM Plex Mono, ui-monospace, SFMono-Regular, monospace"
    fontSize: "1.125rem"
    fontWeight: 600
    lineHeight: 1.1
    letterSpacing: "normal"
  body:
    fontFamily: "IBM Plex Sans, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
rounded:
  sm: "2px"
  md: "4px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "32px"
  xxl: "48px"
components:
  button-primary:
    backgroundColor: "{colors.bg-panel-raised}"
    textColor: "{colors.trace-amber}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
  button-primary-hover:
    backgroundColor: "{colors.bg-panel-raised}"
    textColor: "{colors.trace-amber}"
  button-danger:
    backgroundColor: "{colors.bg-panel-raised}"
    textColor: "{colors.threshold-danger}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
---

# Design System: Strip Recorder

## Overview

**Creative North Star: "The Instrument Deck"**

This dashboard is built as an analog seismograph / strip-chart recorder: a
bank of instruments in a near-black housing, each channel a continuous pen
trace on scrolling paper, each risk threshold a literal banded zone the trace
must not cross. It exists to prove one thing on sight: that the system's
fixed risk rails (1% target / 0.25% cap per trade, leverage = 1, 2% daily
halt, 5% drawdown warn, 10% kill-switch) are real, live, and binding — not a
claim buried in a settings page. The three trace colors (amber for equity,
cyan for ML performance, violet for regime state) are semantic channel
identity, not decoration: this is Operate-mode instrumentation, so expression
never outranks the numbers, but a solo quant developer reading three
overlaid signals at 2am needs each one to be instantly, unambiguously
distinguishable by hue.

Rejected on sight: the generic SaaS-admin dashboard (sidebar + KPI-card grid
+ one blue accent + rounded-xl cards + drop shadows). Nothing in this system
uses soft elevation, glassmorphism, or gradient chrome — depth comes from
panel layering and hairline borders, the way a physical instrument's face
plate sits in front of its housing, never from blur or shadow.

**Key Characteristics:**
- Near-black instrument housing, warm off-white numerals, three reserved trace hues
- Monospace-first: every number that could sit in a column does
- Flat panel layering instead of shadows; grain/graticule texture instead of gradients
- Threshold bands are drawn geometry at their real value, never decorative color blocks
- Minimal radius (2-4px) — machined, not soft

## Colors

Near-black instrument housing with a warm off-white numeral color and three
reserved, semantic trace hues; risk-threshold bands use a separate warm/red
pair so a breach is never confused with a data channel.

### Primary
- **Trace Amber** (`#ffb020`): the equity/PnL channel's pen trace ONLY. Also the idle-state accent on primary action buttons (Run Backtest, Start Optuna Sweep), so pressing an action reads as "arming a channel."

### Secondary
- **Trace Cyan** (`#4fd8e0`): the ML-performance (walk-forward oos_r2/dir_acc/ic) channel's pen trace ONLY.

### Tertiary
- **Trace Violet** (`#8b7cf6`): the regime-state channel's pen trace / background band ONLY (Bull/Bear/Sideways, HMM label).

### Neutral
- **Void** (`#0a0b0d`): page background, the space between instrument panels.
- **Panel** (`#121418`): the instrument housing surface — cards, channel-strip backgrounds.
- **Panel Raised** (`#1a1d23`): bezel controls, buttons, and the dock workspace toolbar.
- **Hairline** (`#2a2e37`): 1px borders and graticule grid lines — never 0 or missing.
- **Paper** (`#16181d`): the scrolling "paper roll" trace surface, one step lighter than Panel so it reads as a distinct material.
- **Text Primary** (`#e8e6df`): numerals and bezel labels — warm off-white, never pure white (that would read as a screen, not an instrument).
- **Text Dim** (`#828998`): secondary labels, timestamps, disabled state.

### Named Rules
**The Reserved-Channel Rule.** Trace Amber, Cyan, and Violet are bound one-to-one to the equity/PnL, ML-performance, and regime channels. Amber is additionally allowed on the primary run action because that action arms a recorder channel. Selection, focus, connection, and navigation states use Text Primary or semantic status colors, never a reserved trace hue.

**The Threshold-Is-Not-Decoration Rule.** `threshold-warn` (`#d9a441`) and `threshold-danger` (`#e0483f`) render ONLY as geometry positioned at a real risk-rail value (5% drawdown warn, 10% kill-switch, 2% daily-loss halt) with a bezel-stamped label naming the rule. They never appear as a generic warning/error color elsewhere in the UI.

## Typography

**Display/Label Font:** IBM Plex Mono (with ui-monospace, SFMono-Regular fallback)
**Body Font:** IBM Plex Sans (with system-ui fallback)

**Character:** Monospace for anything that is data, a control label, or a number a reader might scan down a column; sans only for prose (job-config help text, empty-state copy, error messages) where monospace would hurt reading speed for no instrumentation benefit.

### Hierarchy
- **Display** (600, `clamp(1.25rem, 1rem + 1vw, 1.75rem)`, 1.1, mono, 0.02em tracking): panel headers, the current-reading numerals in each channel's right-hand gauge.
- **Label** (600, 0.6875rem, 1.2, mono, 0.12em tracking, uppercase): bezel-stamped control labels, channel names, threshold-band tags. Always uppercase, always tracked wide — this is the "stamped on the housing" register.
- **Metric** (600, 1.125rem, 1.1, mono): compact metric readings and the mobile bezel title.
- **Body** (400, 0.875rem, 1.5, sans): job-config form fields, help text, log console prose lines, empty/error states.

### Named Rules
**The Tabular-Numerals Rule.** Every numeral that could align in a column (equity values, metrics, timestamps, trial numbers) uses `font-variant-numeric: tabular-nums` regardless of font — a strip recorder's readings must line up.

## Layout

Desktop is a single-viewport, TWS-style operating deck. Its compact workspace
toolbar combines workflow navigation, the active workflow description, profile
controls, and authoritative IBKR status without a separate application banner.
A twelve-column by twelve-row dock surface holds the live
chart, model score, risk rails, broker telemetry, news impact, positions, and
model/execution action tape at once. Panels drag from their stamped title bar,
resize from the lower corner, snap to grid cells, and compact when one panel
overtakes another. The default layout uses 4px gutters and deliberately fills
the usable viewport; Balanced and Comfortable density settings increase the
gutter without changing information priority. Every panel owns its scrolling,
so the document itself does not scroll on a desktop workstation. Layout,
visibility, size, and density persist per workflow and asset profile in local
storage. Below 700px the dock becomes a readable single-column stack and the
document may scroll; touch-capable controls retain a 44px minimum target.

## Elevation & Depth

Flat by default. Depth comes from three fixed panel layers (Void → Panel →
Panel Raised) distinguished by a small brightness step plus a 1px Hairline
border, the way a real instrument's bezel sits proud of its housing — never
from box-shadow, blur, or backdrop-filter. The trace/paper surfaces carry a
faint fixed grain texture (a subtle repeating noise pattern, ~4% opacity) for
material authenticity, not glow.

### Named Rules
**The No-Glow Rule.** No `box-shadow`, `filter: drop-shadow`, or `backdrop-filter` anywhere in this system. If something needs to feel "elevated," give it a brighter panel layer and a hairline border, not a shadow.

## Shapes

Minimal, machined radius: 2px (`sm`) on small controls and threshold-band
tabs, 4px (`md`) on panels and buttons — enough to soften a raw right angle,
never enough to read as a soft "SaaS card." Borders are always a visible 1px
Hairline, never a 0-border color block. Status/state indicators (job
running, kill-switch armed) are perfect circles, matching an instrument's
physical indicator lamps.

## Components

### Dock Workspace
- **Structure:** compact control toolbar + bounded 12×12 surface + machined panel frames. The toolbar begins with workflow navigation and the active workflow description, then places profile/session/panel controls and the IBKR indicator at the right. The live-chart pane owns the upper-left majority; model score and risk remain visible at upper right; news runs beneath them; positions and the action tape occupy the lower deck.
- **Panel controls:** the full title bar is the drag handle, the lower-right corner resizes, × hides a panel, and the Panels menu restores hidden panels or resets the canonical TWS layout.
- **Motion:** the grabbed panel follows the pointer directly. Displaced panels slide to their snapped cells with a 240ms exponential ease-out. Reduced-motion users get the same reflow without animation.
- **Persistence:** each workflow/asset-profile pair stores layout coordinates, dimensions, hidden panels, and density locally after changes. Closing the application never resets the deck.

### Buttons
- **Shape:** 4px radius, 1px Hairline border, mono Label typography, uppercase, tracked.
- **Primary** (Run Backtest, Start Optuna Sweep): Panel Raised background, Trace Amber text/border at idle; on hover/focus the border brightens to full-saturation amber and the panel lightens one step — a "switch engaging" feel, never a shadow lift.
- **Danger / Armed** (Start Live Trading): Panel Raised background, threshold-danger text/border at idle, same brighten-on-hover treatment, PLUS a required inline "type to arm" text field that must match the exact confirmation phrase before the button becomes enabled.
- **Ghost** (Cancel job, secondary actions): Text Dim border/text, brightens to Text Primary on hover.

### Channel Strip (signature component)
- **Structure:** left label rail (channel name + current numeric reading, Display typography) + scrolling trace canvas (Paper background, Hairline graticule grid) + optional right-edge threshold-band tabs.
- **Trace:** drawn as an SVG/canvas path in the channel's Reserved Channel color; on load it draws once left-to-right (a "pen advancing" reveal), then stays static — never a looping animation.
- **Accessibility:** every trace exposes a programmatic name plus observation count, current reading, visible range, and threshold summary. The SVG geometry itself is decorative to assistive technology.
- **Threshold bands:** translucent (~15% opacity) horizontal fills in threshold-warn/threshold-danger spanning the strip's full width at the value's real y-position, with a small stamped label tab at the left edge.
- **Comparison mode:** overlaying a second run renders its trace at 40% opacity (ghosted) against the active run's full-opacity trace, same channel color.

### Model Decision Tape
- **Structure:** a real OHLC candlestick pane, translucent green/red model forecast candles, event markers anchored to their decision bar, and a dedicated lower HMM/transition-probability pane. Forecast candles encode the predicted close and use a half-ATR display envelope for the wick; they do not claim independently predicted OHLC. Bull/Bear/Sideways state is a faint background band, never a replacement for the probability traces or text state.
- **Risk references:** an open position's entry, ATR stop, reward/risk target, and risk envelope use labeled references. The bars included in the visible GHMM fit context are enclosed by a violet dashed box. Every tape reads protection from the selected position's authoritative execution state: only an acknowledged broker OCA pair may be labeled active; pending, absent, unmatched, or unknown protection remains explicitly non-guaranteed model reference data.
- **Live contract:** a separate read-only IBKR subscription supplies observed and forming bars; forming bars have a dashed outline. Strategy snapshots replace a same-timestamp warmup point when yhat becomes available. The dashboard polls IB bars every two seconds and strategy telemetry every three seconds, and only merges model points when ticker and cadence match. Arbitrary searched symbols are market-only. The UI must not imply tick-level strategy decisions.
- **Accessibility:** the chart has a programmatic description and the current signal, yhat, HMM state, ATR, threshold, HMM window, timestamp, and protective-order status are also exposed as text.

### Job Console
- **Style:** Paper background, Body typography in Text Primary, monospace timestamps in Text Dim, auto-scrolls to the newest line while a job is running.
- **States:** loading, unknown, empty, running, completed, failed, and cancelled are written in text. Running uses a Text Primary indicator lamp; completed/failed/cancelled use positive/negative/Text Dim. Output auto-scrolls only while the selected job is running.

### Workspace Toolbar
- **Style:** Panel Raised background with a 1px Hairline border. It is the only global control row; there is no separate title banner consuming viewport height.
- **Navigation:** the workflow menu trigger sits immediately before the active Backtest, Optuna, Paper, or Live description so mode changes remain obvious.
- **Connection contract:** the right-edge IBKR indicator says Connecting or Status Unknown until both run and job feeds are confirmed. It never infers Idle from missing data.

## Do's and Don'ts

### Do:
- **Do** reserve Trace Amber / Cyan / Violet exclusively for their one channel each (see the Reserved-Channel Rule).
- **Do** use `tabular-nums` on every column of numbers.
- **Do** position threshold bands at their real risk-rail value, and label every one.
- **Do** pair demonstration live data with an explicit "DEMONSTRATION DATA · NO BROKER CONNECTION" tag, never color alone.
- **Do** render disconnected or stale broker/API state as STATUS UNKNOWN; never substitute an empty or safe reading.
- **Do** respect reduced-motion preferences component by component: stop pulsing lamps and skip trace drawing while retaining textual state.

### Don't:
- **Don't** use box-shadow, blur, backdrop-filter, or glassmorphism anywhere (the No-Glow Rule).
- **Don't** default to rounded-xl/rounded-2xl or pill-shaped controls — this system is machined, not soft.
- **Don't** reuse a Reserved Channel color as a generic UI accent outside its channel.
- **Don't** let the live-trading action button become clickable before the exact confirmation phrase is typed.
