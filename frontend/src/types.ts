export type PaymentStatus = "PENDING" | "PROCESSING" | "COMPLETED" | "FAILED";
export type QueueMessageStatus = "PENDING" | "PROCESSING" | "COMPLETED" | "FAILED";

export interface PaymentMetrics {
  total: number;
  pending: number;
  processing: number;
  completed: number;
  failed: number;
}

export interface PaymentRecord {
  id: string;
  merchant_reference: string;
  amount: string;
  currency: string;
  customer_email: string;
  description: string | null;
  status: PaymentStatus;
  failure_reason: string | null;
  retry_count: number;
  created_at: string;
  updated_at: string;
}

export interface PaymentEventItem {
  id: string;
  payment_id: string;
  queue_name: string;
  event_type: string;
  status: QueueMessageStatus;
  attempts: number;
  last_error: string | null;
  available_at: string;
  created_at: string;
  updated_at: string;
}

export interface PaymentProcessingSnapshot {
  id: string;
  merchant_reference: string;
  status: PaymentStatus;
  retry_count: number;
  failure_reason: string | null;
  latest_event_status: QueueMessageStatus | null;
  latest_event_attempts: number | null;
  latest_event_error: string | null;
}

export interface CreatePaymentInput {
  merchant_reference: string;
  amount: string;
  currency: string;
  customer_email: string;
  description?: string;
}

export interface SessionInfo {
  email: string;
  merchant_id: string;
  csrf_token: string;
}
