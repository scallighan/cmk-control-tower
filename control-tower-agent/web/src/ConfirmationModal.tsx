import { useEffect, useState } from "react";
import { AssessedItem, TradeDetail, getTradeDetail } from "./api";

interface Props {
  item: AssessedItem;
  onClose: () => void;
}

function DetailTable({
  title,
  data,
  highlight,
}: {
  title: string;
  data: Record<string, unknown> | null;
  highlight?: Set<string>;
}) {
  if (!data) {
    return (
      <div className="detail-block">
        <h3>{title}</h3>
        <p className="muted">Not available.</p>
      </div>
    );
  }
  return (
    <div className="detail-block">
      <h3>{title}</h3>
      <table className="detail-table">
        <tbody>
          {Object.entries(data).map(([k, v]) => {
            const hit = highlight?.has(k);
            return (
              <tr key={k} className={hit ? "highlight" : ""}>
                <th>
                  {k}
                  {hit && <span className="flag" title="Value needs adjusting">needs adjusting</span>}
                </th>
                <td className="mono">{v === null ? "—" : String(v)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// Map a confirmation field to the booked-trade field that holds the correct value.
const FIELD_PAIRS: Array<[cfm: string, trade: string]> = [
  ["cfm_price", "price"],
  ["cfm_qty", "qty"],
  ["cfm_gross", "gross_amt"],
];

function computeHighlights(
  confirmation: Record<string, unknown>,
  trade: Record<string, unknown> | null
): { confirmation: Set<string>; trade: Set<string> } {
  const cSet = new Set<string>();
  const tSet = new Set<string>();
  if (confirmation && trade) {
    for (const [cfmKey, tradeKey] of FIELD_PAIRS) {
      const cv = Number(confirmation[cfmKey]);
      const tv = Number(trade[tradeKey]);
      if (Number.isFinite(cv) && Number.isFinite(tv) && cv !== tv) {
        cSet.add(cfmKey);
        tSet.add(tradeKey);
      }
    }
  }
  return { confirmation: cSet, trade: tSet };
}

export default function ConfirmationModal({ item, onClose }: Props) {
  const [detail, setDetail] = useState<TradeDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    getTradeDetail(item.trade_id)
      .then((d) => active && setDetail(d))
      .catch((e) => active && setError((e as Error).message))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [item.trade_id]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Booked trade from the run state (always present); fall back to fetched detail.
  const tradeForHl = (detail?.trade as Record<string, unknown> | null) ?? item.trade;
  const hl = computeHighlights(item.confirmation, tradeForHl);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>
            {item.trade_id}
            <span className={`badge ${item.matched ? "badge-ok" : "badge-warn"}`}>
              {item.matched ? "matched" : "break"}
            </span>
          </h2>
          <button className="close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <div className="modal-body">
          {item.summary && <p className="hl-note">{item.summary}</p>}
          {hl.confirmation.size > 0 && (
            <p className="hl-note">
              Highlighted confirmation fields differ from the booked trade and need adjusting.
            </p>
          )}
          <DetailTable
            title={`Confirmation (from CSV · ${item.source})`}
            data={item.confirmation}
            highlight={hl.confirmation}
          />
          <DetailTable
            title="Booked trade (record)"
            data={detail?.trade ?? item.trade}
            highlight={hl.trade}
          />
          {loading && <p className="muted">Loading securities master &amp; counterparties…</p>}
          {error && <div className="error">⚠ {error}</div>}
          {detail && (
            <>
              <DetailTable title="Securities master" data={detail.security} />
              <DetailTable title="Counterparty — buy side" data={detail.counterparty_buy} />
              <DetailTable title="Counterparty — sell side" data={detail.counterparty_sell} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
