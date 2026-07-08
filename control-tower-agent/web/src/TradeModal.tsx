import { useEffect, useState } from "react";
import { DerivedSignals, TradeDetail, getTradeDetail } from "./api";

interface Props {
  disputeId: string;
  tradeId: string;
  confirmation: Record<string, unknown> | null;
  derived: DerivedSignals | null;
  brokenField: string | null;
  onClose: () => void;
}

// Maps the logical broken field to the confirmation & booked-trade column names.
const FIELD_MAP: Record<string, { cfm: string; trade: string }> = {
  qty: { cfm: "cfm_qty", trade: "qty" },
  price: { cfm: "cfm_price", trade: "price" },
  gross: { cfm: "cfm_gross", trade: "gross_amt" },
};

function KV({
  data,
  hl,
}: {
  data: Record<string, unknown> | null;
  hl?: Set<string>;
}) {
  if (!data) return <p className="muted">Not available.</p>;
  return (
    <table className="detail-table">
      <tbody>
        {Object.entries(data).map(([k, v]) => (
          <tr key={k} className={hl?.has(k) ? "highlight" : ""}>
            <th>
              {k}
              {hl?.has(k) && <span className="flag">needs adjusting</span>}
            </th>
            <td className="mono">{v === null ? "—" : String(v)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="detail-block">
      <h3>{title}</h3>
      {children}
    </div>
  );
}

export default function TradeModal({
  disputeId,
  tradeId,
  confirmation,
  derived,
  brokenField,
  onClose,
}: Props) {
  const [detail, setDetail] = useState<TradeDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getTradeDetail(tradeId)
      .then((d) => active && setDetail(d))
      .catch((e) => active && setError((e as Error).message))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [tradeId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const map = brokenField ? FIELD_MAP[brokenField] : undefined;
  const confHl = new Set<string>();
  const tradeHl = new Set<string>();
  if (map) {
    confHl.add(map.cfm);
    tradeHl.add(map.trade);
  }
  const econ = derived?.economics;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>
            {disputeId}
            <span className="badge badge-cat">{tradeId}</span>
          </h2>
          <button className="close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <div className="modal-body">
          {brokenField && (
            <p className="hl-note">
              The highlighted <b>{brokenField}</b> value differs between the counterparty confirmation
              and the booked trade of record — this is the field the remediation needs to adjust.
              {econ && econ.gross_break_amount != null && (
                <>
                  {" "}
                  Gross break:{" "}
                  {Number(econ.gross_break_amount).toLocaleString("en-US", {
                    style: "currency",
                    currency: "USD",
                  })}
                  .
                </>
              )}
            </p>
          )}

          <Section title="Counterparty confirmation">
            <KV data={confirmation} hl={confHl} />
          </Section>
          <Section title="Booked trade (record of economics)">
            {loading ? (
              <p className="muted">Loading…</p>
            ) : (
              <KV data={detail?.trade ?? null} hl={tradeHl} />
            )}
          </Section>

          {error && <div className="error">⚠ {error}</div>}

          <Section title="Security master">
            {loading ? <p className="muted">Loading…</p> : <KV data={detail?.security ?? null} />}
          </Section>
          {detail && (
            <>
              <Section title="Counterparty — buy side">
                <KV data={detail.counterparty_buy} />
              </Section>
              <Section title="Counterparty — sell side">
                <KV data={detail.counterparty_sell} />
              </Section>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
