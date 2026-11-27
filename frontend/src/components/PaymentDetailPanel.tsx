import type { PaymentEventItem, PaymentRecord } from "../types";
import { formatCurrency, formatDateTime } from "../format";
import { describePayment, providerNotice } from "../paymentPresentation";
import { StatusBadge } from "./StatusBadge";

interface PaymentDetailPanelProps {
  payment: PaymentRecord | null;
  error: string | null;
  events: PaymentEventItem[];
  loading: boolean;
}

export function PaymentDetailPanel({
  payment,
  error,
  events,
  loading,
}: PaymentDetailPanelProps) {
  if (loading && !payment) {
    return (
      <aside className="panel detail-panel">
        <p className="detail-placeholder" role="status">Loading payment details...</p>
      </aside>
    );
  }

  if (!payment) {
    return (
      <aside className="panel detail-panel">
        <p className="detail-placeholder">Select a payment to inspect its status, retries, and event timeline.</p>
      </aside>
    );
  }

  const notice = providerNotice(payment);
  return (
    <aside className="panel detail-panel" aria-busy={loading}>
      <div className="panel-heading">
        <div>
          <p className="section-kicker">Payment Detail</p>
          <h2>{payment.merchant_reference}</h2>
        </div>
        <StatusBadge status={payment.status} />
      </div>

      {error ? <p className="page-error" role="alert">{error}</p> : null}
      <div className="detail-grid">
        <div>
          <span className="detail-label">Customer</span>
          <strong>{payment.customer_email}</strong>
        </div>
        <div>
          <span className="detail-label">Amount</span>
          <strong>{formatCurrency(payment.amount, payment.currency)}</strong>
        </div>
        <div>
          <span className="detail-label">Retries used</span>
          <strong>{payment.retry_count}</strong>
        </div>
        <div>
          <span className="detail-label">Latest queue status</span>
          <strong>{payment.provider_state === "review_required" ? "Paused for review" : events[0]?.status ?? "Awaiting queue activity"}</strong>
        </div>
      </div>

      {payment.provider_reference ? <section className="detail-section">
        <h3>Stripe reference</h3><code className="provider-reference">{payment.provider_reference}</code>
      </section> : null}
      {notice ? <section className="detail-section provider-notice" role="status">
        <h3>{notice.title}</h3><StatusBadge status={notice.label} />
      </section> : null}
      <section className="detail-section">
        <h3>What this means</h3>
        <p>{describePayment(payment)}</p>
      </section>

      {payment.failure_reason ? (
        <section className={`detail-section ${payment.status === "FAILED" ? "danger-callout" : "provider-notice"}`}>
          <h3>{payment.status === "FAILED" ? "Failure information" : "Processing note"}</h3>
          <p>{payment.failure_reason}</p>
        </section>
      ) : null}

      <section className="detail-section">
        <h3>Worker queue history</h3>
        {loading ? <p role="status">Loading queue history...</p> : null}
        <ol className="timeline">
          {events.length === 0 ? (
            <li className="timeline-empty">No queue events recorded yet.</li>
          ) : (
            events.map((event) => (
              <li key={event.id} className="timeline-item">
                <div className="timeline-topline">
                  <strong>{event.event_type}</strong>
                  <StatusBadge status={payment.provider_state === "review_required" && event.status === "FAILED" ? "REVIEW_REQUIRED" : event.status} />
                </div>
                <p>
                  Attempt {event.attempts} · queued for {formatDateTime(event.available_at)}
                </p>
                {event.last_error ? <p className="timeline-error">{event.last_error}</p> : null}
              </li>
            ))
          )}
        </ol>
      </section>
    </aside>
  );
}
