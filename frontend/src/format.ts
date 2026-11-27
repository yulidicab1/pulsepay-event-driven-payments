export function formatCurrency(amount: string, currency: string) {
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency,
      minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(amount));
  } catch {
    return `${currency} ${amount}`;
  }
}

export function formatDateTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Date unavailable";
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit" }).format(date);
}
