import type { PaymentStatus, QueueMessageStatus } from "../types";

interface StatusBadgeProps {
  status: PaymentStatus | QueueMessageStatus | "UNKNOWN" | "REVIEW_REQUIRED" | "REQUIRES_ACTION";
}

export function StatusBadge({ status }: StatusBadgeProps) {
  return <span className={`status-badge status-${status.toLowerCase()}`}>{status.replace(/_/g, " ")}</span>;
}
