import { useState } from "react";
import { api } from "../lib/api.js";
import { dateTime, inr, tone, until } from "../lib/format.js";
import { Avatar } from "../app/Login.jsx";
import { PRODUCTS, VARIETIES } from "../lib/products.js";
import { Badge, ErrorNote, Segmented, Spinner, Stat, useLoad } from "../components/ui.jsx";

const SECTIONS = [
  { id: "details", label: "Details" },
  { id: "permissions", label: "Permissions" },
  { id: "funds", label: "Funds & margins" },
];

function Funds({ title, f }) {
  if (!f) return null;
  return (
    <section className="card">
      <header className="card-head">
        <h3>{title}</h3>
        <Badge tone={f.enabled === false ? "" : "buy"}>{f.enabled === false ? "disabled" : "enabled"}</Badge>
      </header>
      <div className="stats">
        <Stat label="Available margin" value={inr(f.net)} />
        <Stat label="Live balance" value={inr(f.live_balance)} />
        <Stat label="Opening balance" value={inr(f.opening_balance)} />
        <Stat label="Cash" value={inr(f.cash)} />
        <Stat label="Collateral" value={inr(f.collateral)} />
        <Stat label="Intraday pay-in" value={inr(f.intraday_payin)} />
        <Stat label="Used margin" value={inr(f.used)} />
        <Stat label="SPAN" value={inr(f.span)} />
        <Stat label="Exposure" value={inr(f.exposure)} />
        <Stat label="Option premium" value={inr(f.option_premium)} />
        <Stat label="Realised M2M" value={inr(f.m2m_realised)} tone={tone(f.m2m_realised)} />
        <Stat label="Unrealised M2M" value={inr(f.m2m_unrealised)} tone={tone(f.m2m_unrealised)} />
      </div>
    </section>
  );
}

export default function Profile({ session, now, onLogout }) {
  const [section, setSection] = useState("details");
  const profile = useLoad(api.profile);
  const funds = useLoad(api.margins, [], 30_000);
  const p = { ...session, ...(profile.data || {}) };

  return (
    <div className="stack">
      <section className="card profile-hero">
        <Avatar user={p} size={76} />
        <div className="profile-main">
          <h2>{p.user_name || p.user_id}</h2>
          <p className="muted">
            {p.user_id} · {p.email || "email not shared"} · {p.broker || "ZERODHA"}
          </p>
          <div className="chips">
            <span className="chip ok">● Connected · expires in {until(p.expires_at, now)}</span>
            {p.user_type && <span className="chip">{p.user_type}</span>}
          </div>
        </div>
        <button type="button" className="ghost" onClick={onLogout}>
          Log out
        </button>
      </section>

      <Segmented label="Profile section" value={section} onChange={setSection} options={SECTIONS} />
      {profile.loading && <Spinner small />}
      <ErrorNote error={profile.error} />

      {section === "details" && (
        <section className="card fade-in">
          <dl className="facts wide">
            <dt>Full name</dt>
            <dd>{p.user_name || "–"}</dd>
            <dt>Short name</dt>
            <dd>{p.user_shortname || "–"}</dd>
            <dt>Client ID</dt>
            <dd>{p.user_id}</dd>
            <dt>Email</dt>
            <dd>{p.email || "–"}</dd>
            <dt>Broker</dt>
            <dd>{p.broker || "–"}</dd>
            <dt>Account type</dt>
            <dd>{p.user_type || "–"}</dd>
            <dt>Demat consent</dt>
            <dd>{p.demat_consent || "–"}</dd>
            <dt>Kite Connect API key</dt>
            <dd>
              <code>{p.api_key}</code>
            </dd>
            <dt>Logged in</dt>
            <dd>{dateTime(p.logged_in_at)} IST</dd>
            <dt>Session expires</dt>
            <dd>{dateTime(p.expires_at)} IST</dd>
          </dl>
        </section>
      )}

      {section === "permissions" && (
        <div className="stack fade-in">
          <section className="card">
            <div className="perm-grid">
              <div>
                <span className="eyebrow">Exchanges</span>
                <div className="chips">{(p.exchanges || []).map((x) => <span key={x} className="chip">{x}</span>)}</div>
              </div>
              <div>
                <span className="eyebrow">Order types</span>
                <div className="chips">{(p.order_types || []).map((x) => <span key={x} className="chip">{x}</span>)}</div>
              </div>
              <div>
                <span className="eyebrow">Products listed by Kite</span>
                <div className="chips">{(p.products || []).map((x) => <span key={x} className="chip">{x}</span>)}</div>
              </div>
              <div>
                <span className="eyebrow">Order varieties</span>
                <div className="chips">{VARIETIES.map((v) => <span key={v.id} className="chip">{v.label}</span>)}</div>
              </div>
            </div>
          </section>
          <div className="product-cards">
            {PRODUCTS.map((pr) => {
              const on = !p.products?.length || p.products.includes(pr.id);
              return (
                <div key={pr.id} className="card product-card">
                  <div className="product-card-head">
                    <b>{pr.label}</b>
                    <span>{pr.name}</span>
                    <Badge tone={on ? "buy" : "amber"} title={on ? "" : "Kite's profile doesn't list this product; activate it on Kite if orders are rejected"}>
                      {on ? "enabled" : "not listed"}
                    </Badge>
                  </div>
                  <p>{pr.desc}</p>
                  <small className="muted">{pr.exchanges.join(" · ")}</small>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {section === "funds" && (
        <div className="stack fade-in">
          <Funds title="Equity" f={funds.data?.equity} />
          <Funds title="Commodity" f={funds.data?.commodity} />
          {funds.loading && !funds.data && <Spinner />}
          <ErrorNote error={funds.error} />
        </div>
      )}
    </div>
  );
}
