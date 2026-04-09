import type { PaymentStatus, QueueMessageStatus } from "../types";

interface StatusBadgeProps {
  status: PaymentStatus | QueueMessageStatus;
}

export function StatusBadge({ status }: StatusBadgeProps) {
  return <span className={`status-badge status-${status.toLowerCase()}`}>{status}</span>;
}
