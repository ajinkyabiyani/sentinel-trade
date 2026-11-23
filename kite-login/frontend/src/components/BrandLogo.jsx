// Sentinel Trade mark: a rounded clay square with a rising price line that ends in a point. Colours follow the theme.
export default function BrandLogo({ size = 36 }) {
  return (
    <svg className="brand-logo" width={size} height={size} viewBox="0 0 32 32" role="img" aria-label="Sentinel Trade">
      <rect x="1" y="1" width="30" height="30" rx="8" style={{ fill: "var(--accent)" }} />
      <path d="M7.5 21.5 12.6 16.4l3.6 3.2 7.6-8.1" fill="none" stroke="#fff" strokeWidth="2.3" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="23.8" cy="11.5" r="2.1" fill="#fff" />
    </svg>
  );
}
