import type { PaymentRecord, PaymentStatus } from "../types";
import { StatusBadge } from "./StatusBadge";

interface PaymentTableProps {
  payments: PaymentRecord[];
  activeStatus: PaymentStatus | "ALL";
  selectedPaymentId: string | null;
  onStatusChange: (status: PaymentStatus | "ALL") => void;
  onSelectPayment: (paymentId: string) => void;
}

const filterOptions: Array<PaymentStatus | "ALL"> = ["ALL", "PENDING", "PROCESSING", "COMPLETED", "FAILED"];

export function PaymentTable({
  payments,
  activeStatus,
  selectedPaymentId,
  onStatusChange,
  onSelectPayment,
}: PaymentTableProps) {
  return (
    <section className="panel payments-panel">
      <div className="panel-heading">
        <div>
          <p className="section-kicker">Payments Queue</p>
          <h2>Track what is flowing through the platform</h2>
        </div>
        <div className="filter-row" role="tablist" aria-label="Filter payments by status">
          {filterOptions.map((status) => (
            <button
              key={status}
              type="button"
              className={status === activeStatus ? "filter-chip active" : "filter-chip"}
              onClick={() => onStatusChange(status)}
            >
              {status === "ALL" ? "All payments" : status}
            </button>
          ))}
        </div>
      </div>

      <div className="table-wrapper">
        <table className="payments-table">
          <thead>
            <tr>
              <th>Reference</th>
              <th>Status</th>
              <th>Customer</th>
              <th>Amount</th>
              <th>Retries</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {payments.length === 0 ? (
              <tr>
                <td colSpan={6} className="empty-state-cell">
                  No payments match this filter yet.
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
                      <strong>{payment.merchant_reference}</strong>
                      <span>{payment.description || "No description provided"}</span>
                    </div>
                  </td>
                  <td>
                    <StatusBadge status={payment.status} />
                  </td>
                  <td>{payment.customer_email}</td>
                  <td>{formatCurrency(payment.amount, payment.currency)}</td>
                  <td>{payment.retry_count}</td>
                  <td>{formatDate(payment.created_at)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function formatCurrency(amount: string, currency: string) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
  }).format(Number(amount));
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}
