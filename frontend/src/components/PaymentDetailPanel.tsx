import type { PaymentEventItem, PaymentProcessingSnapshot, PaymentRecord } from "../types";
import { StatusBadge } from "./StatusBadge";

interface PaymentDetailPanelProps {
  payment: PaymentRecord | null;
  processing: PaymentProcessingSnapshot | null;
  events: PaymentEventItem[];
  loading: boolean;
}

export function PaymentDetailPanel({
  payment,
  processing,
  events,
  loading,
}: PaymentDetailPanelProps) {
  if (loading) {
    return (
      <aside className="panel detail-panel">
        <p className="detail-placeholder">Loading payment details...</p>
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

  return (
    <aside className="panel detail-panel">
      <div className="panel-heading">
        <div>
          <p className="section-kicker">Payment Detail</p>
          <h2>{payment.merchant_reference}</h2>
        </div>
        <StatusBadge status={payment.status} />
      </div>

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
          <strong>{processing?.retry_count ?? payment.retry_count}</strong>
        </div>
        <div>
          <span className="detail-label">Latest queue status</span>
          <strong>{processing?.latest_event_status ?? "Awaiting queue activity"}</strong>
        </div>
      </div>

      <section className="detail-section">
        <h3>What this means</h3>
        <p>{describeStatus(payment.status)}</p>
      </section>

      {processing?.failure_reason ? (
        <section className="detail-section danger-callout">
          <h3>Failure information</h3>
          <p>{processing.failure_reason}</p>
          {processing.latest_event_error ? <p>Latest worker note: {processing.latest_event_error}</p> : null}
        </section>
      ) : null}

      <section className="detail-section">
        <h3>Status history</h3>
        <ol className="timeline">
          {events.length === 0 ? (
            <li className="timeline-empty">No queue events recorded yet.</li>
          ) : (
            events.map((event) => (
              <li key={event.id} className="timeline-item">
                <div className="timeline-topline">
                  <strong>{event.event_type}</strong>
                  <StatusBadge status={event.status} />
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

function formatCurrency(amount: string, currency: string) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
  }).format(Number(amount));
}

function formatDateTime(value: string) {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

function describeStatus(status: PaymentRecord["status"]) {
  if (status === "COMPLETED") {
    return "The payment has cleared processing and is ready to count as a successful transaction.";
  }
  if (status === "FAILED") {
    return "The payment reached a terminal failure state and will not continue without intervention.";
  }
  if (status === "PROCESSING") {
    return "The worker is actively processing this payment right now.";
  }
  return "The payment is waiting in the queue for processing or for a retry attempt.";
}
