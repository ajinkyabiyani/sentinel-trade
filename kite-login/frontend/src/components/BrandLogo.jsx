import { useId } from "react";

// Sentinel Trade mark: a sentinel's shield in cyan with "ST" set in it and a gold tick of a rising price
// line crossing the shoulder of the S.
export default function BrandLogo({ size = 36 }) {
  const id = useId().replace(/:/g, "");
  return (
    <svg className="brand-logo" width={size} height={size} viewBox="0 0 32 32" role="img" aria-label="Sentinel Trade">
      <defs>
        <linearGradient id={`st${id}`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#6fdeef" />
          <stop offset="1" stopColor="#1497ad" />
        </linearGradient>
        <linearGradient id={`sg${id}`} x1="0" y1="1" x2="1" y2="0">
          <stop offset="0" stopColor="#f59f1b" />
          <stop offset="1" stopColor="#ffe066" />
        </linearGradient>
      </defs>
      <path d="M16 1.8 28 6v9.2c0 7.3-5 12.6-12 15-7-2.4-12-7.7-12-15V6z" fill="#0f1826" stroke={`url(#st${id})`} strokeWidth="1.6" strokeLinejoin="round" />
      <text x="16" y="20.6" textAnchor="middle" fontFamily="Inter, Segoe UI, Arial, sans-serif" fontWeight="800" fontSize="11.5" letterSpacing="-0.4" fill={`url(#st${id})`}>ST</text>
      <path d="M8.5 25.2 13.4 21.6l3.1 1.8 6.6-5.3" fill="none" stroke={`url(#sg${id})`} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
