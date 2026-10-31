/**
 * Lucent v1 Home Assistant adapter, shared by every Roborock card on the Robots dashboard.
 * Colours are the HA theme's own (Lucent "host theme first" rule); import these into each
 * card's <style> so all cards stay identical.
 */

export const LUCENT_TOKENS = `
:host {
  /* Lucent v1 Home Assistant adapter: every colour is the HA theme's own. */
  --lu-accent: var(--primary-color);
  --lu-accent-ink: var(--text-primary-color, #fff);
  --lu-ink: var(--primary-text-color);
  --lu-ink-2: var(--secondary-text-color);
  --lu-ink-3: var(--disabled-text-color, var(--secondary-text-color));
  --lu-danger: var(--error-color, #db4437);
  --lu-warning: var(--warning-color, #ffa600);
  --lu-card: var(--ha-card-background, var(--card-background-color));
  --lu-edge: var(--ha-card-border-color, var(--divider-color));
  --lu-tile: color-mix(in srgb, var(--primary-text-color) 6%, transparent);
  --lu-glass-raised: color-mix(in srgb, var(--primary-text-color) 12%, transparent);
  --lu-edge-raised: color-mix(in srgb, var(--primary-text-color) 22%, transparent);
  --lu-scrim: color-mix(in srgb, var(--primary-background-color) 55%, transparent);
  --lu-float: color-mix(in srgb, var(--card-background-color, #fff) 78%, transparent);
  --lu-radius-card: var(--ha-card-border-radius, 24px);
  --lu-radius-tile: max(calc(var(--lu-radius-card) - 4px), 8px);
  --lu-radius-control: max(calc(var(--lu-radius-card) - 10px), 6px);
  --lu-radius-pill: 999px;
  --lu-target: 48px;
  --lu-highlight-raised: inset 0 1px 0 color-mix(in srgb, #fff 18%, transparent);
  --lu-shadow-raised: 0 10px 24px color-mix(in srgb, #000 22%, transparent);
  --lu-shadow-pressed: 0 4px 10px color-mix(in srgb, #000 18%, transparent);
  --lu-ease: cubic-bezier(.33, 1, .68, 1);
  --lu-motion-press: 90ms;
  --lu-motion-focus: 150ms;
  --lu-motion-layer: 220ms;
  --lu-font: var(--ha-font-family-body, var(--paper-font-body1_-_font-family, inherit));
  display: block;
}
@media (prefers-reduced-motion: reduce) {
  :host { --lu-motion-focus: 0ms; --lu-motion-layer: 120ms; }
}
`;

// Shared Lucent controls: base button rules and the segmented toggle.
export const LUCENT_CONTROLS = `
button { font: inherit; color: inherit; cursor: pointer; -webkit-tap-highlight-color: transparent; }
button:disabled { cursor: default; }
button:focus-visible { outline: 2px solid var(--lu-accent); outline-offset: 2px; }
[hidden] { display: none !important; }
.segmented {
  display: inline-flex; gap: 2px; padding: 3px;
  border-radius: var(--lu-radius-pill);
  border: 1px solid var(--lu-edge);
}
.segmented button {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  min-height: 40px; padding: 0 14px;
  border: 0; border-radius: var(--lu-radius-pill);
  background: transparent;
  color: var(--lu-ink-2);
  font-size: 14px; font-weight: 500;
  --mdc-icon-size: 18px;
  transition: background var(--lu-motion-focus) var(--lu-ease), color var(--lu-motion-focus) var(--lu-ease), transform var(--lu-motion-press) var(--lu-ease);
}
.segmented button:active { transform: scale(.97); }
.segmented button[aria-checked="true"] {
  background: var(--lu-accent);
  color: var(--lu-accent-ink);
  font-weight: 600;
}
`;
