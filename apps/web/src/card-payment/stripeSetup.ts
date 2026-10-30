import type { BillingInput, SavedCardState, SavedCardStatus } from "../api/client";

export interface StripeSetupError {
  message?: string;
}

export interface StripeSetupElements {
  submit: () => Promise<{ error?: StripeSetupError }>;
}

export interface StripeSetupClient {
  confirmSetup: (input: {
    elements: StripeSetupElements;
    confirmParams: {
      return_url: string;
      payment_method_data: {
        billing_details: {
          name: string;
          email?: string;
          address: {
            line1: string;
            city: string;
            state: string;
            postal_code: string;
            country: string;
          };
        };
      };
    };
    redirect: "if_required";
  }) => Promise<{
    setupIntent?: { id: string; status?: string };
    error?: StripeSetupError;
  }>;
}

export class SavedCardStatusUnknownError extends Error {
  constructor() {
    super("银行验证结果尚未核实，请先查询保存状态，再继续验证。");
    this.name = "SavedCardStatusUnknownError";
  }
}

export function isSavedCard(status: Pick<SavedCardStatus, "state"> | null): boolean {
  return status?.state === "saved" || status?.state === "checkout_ready";
}

export function isSavedCardPending(state?: SavedCardState): boolean {
  return state === "processing" || state === "sync_pending";
}

/** Confirmation never decides whether a card is saved; only the account status API does. */
export async function confirmSavedCardSetup(input: {
  stripe: StripeSetupClient;
  elements: StripeSetupElements;
  billing: BillingInput;
  returnUrl: string;
  readStatus: () => Promise<SavedCardStatus>;
}): Promise<{ status: SavedCardStatus; confirmationError?: string }> {
  const submitted = await input.elements.submit();
  if (submitted?.error) throw new Error(submitted.error.message || "请检查卡片信息");

  let confirmationError: string | undefined;
  try {
    const result = await input.stripe.confirmSetup({
      elements: input.elements,
      confirmParams: {
        return_url: input.returnUrl,
        payment_method_data: {
          billing_details: {
            name: input.billing.name,
            email: input.billing.email || undefined,
            address: {
              line1: input.billing.line1,
              city: input.billing.city,
              state: input.billing.state,
              postal_code: input.billing.postal_code,
              country: input.billing.country,
            },
          },
        },
      },
      redirect: "if_required",
    });
    confirmationError = result.error?.message;
    if (result.error && !confirmationError) confirmationError = "银行验证未完成，请核对保存状态。";
  } catch (error) {
    confirmationError = error instanceof Error ? error.message : "验证连接中断，请核对保存状态。";
  }

  try {
    // This read also reconciles an unknown confirmation result. Confirmation is never replayed here.
    return { status: await input.readStatus(), confirmationError };
  } catch {
    throw new SavedCardStatusUnknownError();
  }
}

export function savedCardReturnUrl(locationUrl: string, orderId: string): string {
  const current = new URL(locationUrl);
  const result = new URL(current.pathname, current.origin);
  result.searchParams.set("setup_order", orderId);
  return result.toString();
}

export function readSavedCardReturn(locationUrl: string): { orderId: string; cleanUrl: string } | null {
  const url = new URL(locationUrl);
  const orderId = url.searchParams.get("setup_order");
  if (!orderId) return null;
  for (const name of ["setup_intent", "setup_intent_client_secret", "client_secret", "redirect_status"]) {
    url.searchParams.delete(name);
  }
  return { orderId, cleanUrl: `${url.pathname}${url.search}${url.hash}` };
}
