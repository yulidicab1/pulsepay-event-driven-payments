import type { PaymentRecord, PaymentFilter } from "../types";
import { formatCurrency, formatDateTime } from "../format";
import { providerNotice } from "../paymentPresentation";
import { StatusBadge } from "./StatusBadge";

interface PaymentTableProps {
  payments: PaymentRecord[];
  loading: boolean;
  unavailable: boolean;
  activeStatus: PaymentFilter;
  selectedPaymentId: string | null;
  onStatusChange: (status: PaymentFilter) => void;
  onSelectPayment: (paymentId: string) => void;
}

const filterOptions: Array<PaymentFilter> = ["ALL", "PENDING", "PROCESSING", "COMPLETED", "FAILED", "UNKNOWN", "REVIEW_REQUIRED"];

export function PaymentTable({
  payments,
  loading,
  unavailable,
  activeStatus,
  selectedPaymentId,
  onStatusChange,
  onSelectPayment,
}: PaymentTableProps) {
  return (
    <section className="panel payments-panel" aria-busy={loading}>
      <div className="panel-heading">
        <div>
          <p className="section-kicker">Payments Queue</p>
          <h2>Track what is flowing through the platform</h2>
        </div>
        <div className="filter-row" role="group" aria-label="Filter payments by status">
          {filterOptions.map((status) => (
            <button
              key={status}
              type="button"
              aria-pressed={status === activeStatus}
              className={status === activeStatus ? "filter-chip active" : "filter-chip"}
              onClick={() => onStatusChange(status)}
            >
              {status === "ALL" ? "All payments" : status.replace(/_/g, " ")}
            </button>
          ))}
        </div>
      </div>

      <div className="table-wrapper" role="region" aria-label="Payments table" tabIndex={0}>
        <table className="payments-table">
          <caption className="sr-only">Payments for your merchant. Select a reference to view details.</caption>
          <thead>
            <tr>
              <th scope="col">Reference</th>
              <th scope="col">Status</th>
              <th scope="col">Customer</th>
              <th scope="col">Amount</th>
              <th scope="col">Retries</th>
              <th scope="col">Created</th>
            </tr>
          </thead>
          <tbody>
            {payments.length === 0 ? (
              <tr>
                <td colSpan={6} className="empty-state-cell">
                  {loading ? "Loading payments..." : unavailable ? "Payment data is currently unavailable." : "No payments match this filter yet."}
                </td>
              </tr>
            ) : (
              payments.map((payment) => (
                <tr
                  key={payment.id}
                  className={payment.id === selectedPaymentId ? "selected" : undefined}
                  onClick={() => onSelectPayment(payment.id)}
                >
                  <td>
                    <div className="reference-cell">
                      <button type="button" className="reference-button"
                        aria-label={`View payment ${payment.merchant_reference}`} aria-pressed={payment.id === selectedPaymentId}
                        onClick={(event) => { event.stopPropagation(); onSelectPayment(payment.id); }}>
                        <strong>{payment.merchant_reference}</strong>
                      </button>
                      <span>{payment.description || "No description provided"}</span>
                      {payment.provider_reference ? <code className="provider-reference">{payment.provider_reference}</code> : null}
                    </div>
                  </td>
                  <td>
                    <StatusBadge status={payment.status} />
                    {providerNotice(payment) ? <div className="provider-summary"><StatusBadge status={providerNotice(payment)!.label} /></div> : null}
                  </td>
                  <td>{payment.customer_email}</td>
                  <td>{formatCurrency(payment.amount, payment.currency)}</td>
                  <td>{payment.retry_count}</td>
                  <td>{formatDateTime(payment.created_at)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
