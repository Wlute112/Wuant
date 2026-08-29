import "./asset-profile-switch.css";

export default function AssetProfileSwitch({ value, profiles, onChange, compact = false }) {
  const selected = profiles[value];
  return (
    <section className={`asset-profile ${compact ? "is-compact" : ""}`} aria-labelledby="asset-profile-title">
      <div className="asset-profile__heading">
        <div className={compact ? "sr-only" : ""}>
          <h2 id="asset-profile-title" className="label">Operating profile</h2>
          <p>{selected.description}</p>
        </div>
        <div className="asset-profile__switch" role="radiogroup" aria-label="Trading asset profile">
          {Object.values(profiles).map((profile) => (
            <button
              key={profile.asset_class}
              type="button"
              role="radio"
              aria-checked={value === profile.asset_class}
              className={value === profile.asset_class ? "is-active" : ""}
              onClick={() => onChange(profile.asset_class)}
            >
              <span>{profile.short_label}</span>
              <small>{profile.scoring.short_label} CORE</small>
            </button>
          ))}
        </div>
      </div>
      {!compact && <dl className="asset-profile__facts">
        <div>
          <dt>Score backbone</dt>
          <dd>{selected.scoring.label} − fill activity</dd>
        </div>
        <div>
          <dt>Session</dt>
          <dd>{selected.market.session}</dd>
        </div>
        <div>
          <dt>Routing / Price</dt>
          <dd>{selected.market.venue} / {selected.market.price_type}</dd>
        </div>
        <div>
          <dt>Quantity</dt>
          <dd>{selected.market.quantity}</dd>
        </div>
        <div>
          <dt>Fee assumption</dt>
          <dd>{selected.market.fee_model}</dd>
        </div>
      </dl>}
      {!compact && selected.warnings?.length > 0 && (
        <ul className="asset-profile__warnings" aria-label={`${selected.label} operating considerations`}>
          {selected.warnings.map((warning) => <li key={warning}>{warning}</li>)}
        </ul>
      )}
    </section>
  );
}
