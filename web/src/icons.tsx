// Minimal inline icon set (no external deps) — 16x16, currentColor.

interface P {
  size?: number;
}
const S = (p: P) => ({ width: p.size ?? 16, height: p.size ?? 16, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", strokeWidth: 1.6, strokeLinecap: "round" as const, strokeLinejoin: "round" as const });

export const IconHome = (p: P) => (
  <svg {...S(p)}><path d="M2 7.5 8 2.5 14 7.5" /><path d="M3.5 6.8V13.5h2.6v-3h3.3v3h2.6V6.8" /></svg>
);
export const IconScan = (p: P) => (
  <svg {...S(p)}><rect x="2.5" y="2.5" width="4" height="4" rx="0.7" /><rect x="9.5" y="2.5" width="4" height="4" rx="0.7" /><rect x="2.5" y="9.5" width="4" height="4" rx="0.7" /><rect x="9.5" y="9.5" width="4" height="4" rx="0.7" /></svg>
);
export const IconChart = (p: P) => (
  <svg {...S(p)}><path d="M2 13.5h12" /><path d="M3 10.5 6 7l2.5 2L13 4.5" /><path d="M9.5 4.5h3.5V8" /></svg>
);
export const IconBell = (p: P) => (
  <svg {...S(p)}><path d="M3.5 11.5c.7-.6 1-1.5 1-2.9V6a3.5 3.5 0 0 1 7 0v2.6c0 1.4.3 2.3 1 2.9" /><path d="M6.2 12.7a1.9 1.9 0 0 0 3.6 0" /></svg>
);
export const IconMail = (p: P) => (
  <svg {...S(p)}><rect x="2" y="3.5" width="12" height="9" rx="1.5" /><path d="m2.5 5 5.5 3.7L13.5 5" /></svg>
);
export const IconBrief = (p: P) => (
  <svg {...S(p)}><path d="M4 2.5h8v11H4z" /><path d="M6 5.5h4M6 7.8h4M6 10h2" /></svg>
);
export const IconLayers = (p: P) => (
  <svg {...S(p)}><path d="M8 2 14 5.5 8 9 2 5.5Z" /><path d="M2 10.5 8 14l6-3.5" /><path d="M2 7.8 8 11l6-3.2" /></svg>
);
export const IconShield = (p: P) => (
  <svg {...S(p)}><path d="M8 1.8 13.5 3.7v4c0 3.6-2.4 5.9-5.5 7-3.1-1.1-5.5-3.4-5.5-7v-4Z" /><path d="M5.7 8l1.7 1.7 3-3.2" /></svg>
);
export const IconSun = (p: P) => (
  <svg {...S(p)}><circle cx="8" cy="8" r="2.8" /><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.2 3.2l1.4 1.4M11.4 11.4l1.4 1.4M12.8 3.2l-1.4 1.4M4.6 11.4l-1.4 1.4" /></svg>
);
export const IconMoon = (p: P) => (
  <svg {...S(p)}><path d="M13.5 9.2A5.5 5.5 0 0 1 6.8 2.5a5.5 5.5 0 1 0 6.7 6.7Z" /></svg>
);
export const IconRefresh = (p: P) => (
  <svg {...S(p)}><path d="M2.5 8a5.5 5.5 0 0 1 9.6-3.4M13.5 8a5.5 5.5 0 0 1-9.6 3.4" /><path d="M12.5 1.5V5H9M3.5 14.5V11H7" /></svg>
);
export const IconChevL = (p: P) => (
  <svg {...S(p)}><path d="M9.5 3.5 5.5 8l4 4.5" /></svg>
);
export const IconChevR = (p: P) => (
  <svg {...S(p)}><path d="M6.5 3.5l4 4.5-4 4.5" /></svg>
);
export const IconFlask = (p: P) => (
  <svg {...S(p)}><path d="M6.5 2.2v3.6L3.4 11.4a1.6 1.6 0 0 0 1.4 2.4h6.4a1.6 1.6 0 0 0 1.4-2.4L9.5 5.8V2.2" /><path d="M5.6 2.2h4.8" /><path d="M4.9 9.6h6.2" /></svg>
);
export const IconPulse = (p: P) => (
  <svg {...S(p)}><path d="M1.5 8h2.7L6 3.6 8.4 12l2.1-4h4" /></svg>
);
export const IconServer = (p: P) => (
  <svg {...S(p)}><rect x="2.2" y="2.6" width="11.6" height="4.6" rx="1.2" /><rect x="2.2" y="8.8" width="11.6" height="4.6" rx="1.2" /><path d="M4.6 4.9h.01M4.6 11.1h.01" /></svg>
);
export const IconCrosshair = (p: P) => (
  <svg {...S(p)}><circle cx="8" cy="8" r="5" /><path d="M8 1.5v3M8 11.5v3M1.5 8h3M11.5 8h3" /></svg>
);
