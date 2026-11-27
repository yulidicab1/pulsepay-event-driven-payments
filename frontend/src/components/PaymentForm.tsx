import { useState, type FormEvent } from "react";

import type { CreatePaymentInput } from "../types";

interface PaymentFormProps {
  submitting: boolean;
  error: string | null;
  confirmation: string | null;
  onSubmit: (payload: CreatePaymentInput) => Promise<boolean>;
}

const initialFormState: CreatePaymentInput = {
  merchant_reference: "",
  amount: "",
  currency: "USD",
  customer_email: "",
  description: "",
};

export function PaymentForm({ submitting, error, confirmation, onSubmit }: PaymentFormProps) {
  const [formState, setFormState] = useState<CreatePaymentInput>(initialFormState);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const saved = await onSubmit({
      ...formState,
      currency: formState.currency.toUpperCase(),
      description: formState.description?.trim() || undefined,
    });
    if (saved) setFormState(initialFormState);
  }

  function updateField<Key extends keyof CreatePaymentInput>(field: Key, value: CreatePaymentInput[Key]) {
    setFormState((current) => ({
      ...current,
      [field]: value,
    }));
  }

  return (
    <section className="panel payment-form-panel">
      <div className="panel-heading">
        <div>
          <p className="section-kicker">Create Payment</p>
          <h2>Send a new payment into the processing flow</h2>
        </div>
        <p className="section-note">New dashboard payments use the simulator and update asynchronously.</p>
      </div>

      <form className="payment-form" onSubmit={handleSubmit}>
        <label>
          Merchant reference
          <input
            disabled={submitting}
            required
            minLength={3}
            maxLength={64}
            value={formState.merchant_reference}
            onChange={(event) => updateField("merchant_reference", event.target.value)}
            placeholder="order-2026-001"
          />
        </label>

        <label>
          Amount
          <input
            disabled={submitting}
            required
            type="number"
            min="0.01"
            step="0.01"
            value={formState.amount}
            onChange={(event) => updateField("amount", event.target.value)}
            placeholder="149.99"
          />
        </label>

        <label>
          Currency
          <input
            disabled={submitting}
            required
            minLength={3}
            maxLength={3}
            value={formState.currency}
            onChange={(event) => updateField("currency", event.target.value)}
            placeholder="USD"
          />
        </label>

        <label>
          Customer email
          <input
            disabled={submitting}
            required
            type="email"
            value={formState.customer_email}
            onChange={(event) => updateField("customer_email", event.target.value)}
            placeholder="buyer@company.com"
          />
        </label>

        <label className="form-field-wide">
          Description
          <textarea
            disabled={submitting}
            rows={3}
            value={formState.description}
            onChange={(event) => updateField("description", event.target.value)}
            placeholder="VIP ticket, annual membership, sponsor package..."
          />
        </label>

        <div className="form-footer">
          <p className="form-helper">
            Tip: use <code>retry</code> in the merchant reference to simulate a retry, or <code>fail</code> to simulate a hard failure.
          </p>
          <button type="submit" disabled={submitting}>
            {submitting ? "Creating..." : "Create payment"}
          </button>
        </div>

        {error ? <p className="form-error" role="alert">{error}</p> : null}
        {confirmation ? <p className="form-confirmation" role="status">{confirmation}</p> : null}
      </form>
    </section>
  );
}
