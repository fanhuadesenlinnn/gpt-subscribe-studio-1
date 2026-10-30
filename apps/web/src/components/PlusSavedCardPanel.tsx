import { Alert, Badge, Button, Group, Paper, SimpleGrid, Stack, Text, TextInput } from "@mantine/core";
import { useCallback, useEffect, useId, useRef, useState } from "react";

import {
  getPlusSavedCard,
  preparePlusSavedCard,
  refreshPlusSavedCardCheckout,
  type BillingInput,
  type Order,
  type SavedCardPreparation,
  type SavedCardState,
  type SavedCardStatus,
} from "../api/client";
import { loadStripe } from "../card-payment/CardFields";
import {
  confirmSavedCardSetup,
  isSavedCard,
  isSavedCardPending,
  savedCardReturnUrl,
  SavedCardStatusUnknownError,
  type StripeSetupClient,
  type StripeSetupElements,
} from "../card-payment/stripeSetup";
import { CheckoutLinkCard } from "./CheckoutLinkCard";

type Work = "preparing" | "saving" | "checking" | "refreshing" | "verifying";
type MountedSetup = { stripe: StripeSetupClient; elements: StripeSetupElements };
type PaymentElement = { unmount: () => void };

interface Props {
  order: Order;
  disabled?: boolean;
  onBusyChange?: (busy: boolean) => void;
  onWorkingChange?: (working: boolean) => void;
  onVerify?: () => Promise<void>;
  onOrder: (order: Order) => void;
}

const STATUS_LABELS: Record<SavedCardState, string> = {
  not_started: "尚未保存付款方式",
  requires_payment_method: "待填写卡片",
  requires_confirmation: "待确认保存",
  requires_action: "待完成银行验证",
  processing: "银行正在处理",
  canceled: "本次卡片设置已取消",
  sync_pending: "等待账号同步付款方式",
  saved: "付款方式已保存，待重新提链",
  checkout_ready: "付款方式已保存，结账链接已刷新",
  prepare_unknown: "卡片准备结果待核实",
};

export function PlusSavedCardPanel({ order, disabled = false, onBusyChange, onWorkingChange, onVerify, onOrder }: Props) {
  const [status, setStatus] = useState<SavedCardStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [setup, setSetup] = useState<SavedCardPreparation | null>(null);
  const [ready, setReady] = useState(false);
  const [work, setWork] = useState<Work | null>(null);
  const [needsStatusCheck, setNeedsStatusCheck] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshedUrl, setRefreshedUrl] = useState<string | null>(null);
  const [billing, setBilling] = useState<BillingInput>(() => ({
    name: "", email: order.session_email || "", line1: "", city: "", state: "", postal_code: "",
    country: order.billing_country || "US",
  }));
  const targetId = `plus-setup-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const mountedSetup = useRef<MountedSetup | null>(null);
  const workInProgress = useRef(false);
  const epoch = useRef(0);
  const onBusyRef = useRef(onBusyChange);
  const onWorkingRef = useRef(onWorkingChange);
  const onOrderRef = useRef(onOrder);
  onBusyRef.current = onBusyChange;
  onWorkingRef.current = onWorkingChange;
  onOrderRef.current = onOrder;

  const applyStatus = useCallback((next: SavedCardStatus) => {
    setStatus(next);
    setNeedsStatusCheck(false);
    if (isSavedCard(next)) setSetup(null);
  }, []);

  useEffect(() => {
    const current = ++epoch.current;
    const controller = new AbortController();
    setStatusLoading(true);
    void getPlusSavedCard(order.id, controller.signal)
      .then((next) => { if (epoch.current === current) applyStatus(next); })
      .catch((err: unknown) => {
        if (epoch.current === current) setError(err instanceof Error ? err.message : "保存状态读取失败");
      })
      .finally(() => { if (epoch.current === current) setStatusLoading(false); });
    return () => {
      epoch.current += 1;
      controller.abort();
      onBusyRef.current?.(false);
      onWorkingRef.current?.(false);
    };
  }, [order.id, applyStatus]);

  // An unfinished setup keeps the old checkout payment button locked until a refreshed checkout exists.
  const locksPayment = statusLoading || work !== null || needsStatusCheck || !status
    || (status.state !== "not_started" && status.state !== "checkout_ready");
  useEffect(() => { onBusyRef.current?.(locksPayment); }, [locksPayment]);
  useEffect(() => { onWorkingRef.current?.(work !== null); }, [work]);

  useEffect(() => {
    if (!setup) {
      mountedSetup.current = null;
      setReady(false);
      return;
    }
    let stopped = false;
    let paymentElement: PaymentElement | null = null;
    setReady(false);
    void (async () => {
      try {
        await loadStripe("v3");
        if (stopped) return;
        if (!window.Stripe) throw new Error("Stripe.js 加载失败");
        const stripe = window.Stripe(setup.publishable_key);
        if (!stripe.confirmSetup) throw new Error("当前 Stripe.js 未提供卡片保存方法，请刷新页面重试。");
        const elements = stripe.elements({
          clientSecret: setup.client_secret,
          appearance: { theme: "stripe", variables: { borderRadius: "8px" } },
        });
        const payment = elements.create("payment", {
          layout: "tabs",
          fields: { billingDetails: { name: "never", email: "never", address: "never" } },
          wallets: { applePay: "never", googlePay: "never" },
        });
        paymentElement = payment;
        const handle = { stripe: { confirmSetup: stripe.confirmSetup.bind(stripe) }, elements };
        const markReady = () => {
          if (stopped) return;
          mountedSetup.current = handle;
          setReady(true);
        };
        payment.on?.("ready", markReady);
        payment.on?.("loaderror", (event: { error?: { message?: string } }) => {
          if (stopped) return;
          mountedSetup.current = null;
          setReady(false);
          setError(event.error?.message || "Stripe 安全输入框加载失败");
        });
        payment.mount(`#${targetId}`);
        if (!payment.on) markReady();
      } catch (err) {
        if (!stopped) setError(err instanceof Error ? err.message : "Stripe 安全输入框加载失败");
      }
    })();
    return () => {
      stopped = true;
      mountedSetup.current = null;
      paymentElement?.unmount();
    };
  }, [setup, targetId]);

  useEffect(() => {
    if (!isSavedCardPending(status?.state)) return;
    let stopped = false;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const next = await getPlusSavedCard(order.id, controller.signal);
        if (stopped) return;
        applyStatus(next);
        attempts += 1;
        if (isSavedCardPending(next.state) && attempts < 12) timer = setTimeout(() => void poll(), 2500);
      } catch (err) {
        if (!stopped) setError(err instanceof Error ? err.message : "保存状态读取失败，请点击查询保存状态。");
      }
    };
    timer = setTimeout(() => void poll(), 2500);
    return () => { stopped = true; controller.abort(); clearTimeout(timer); };
  }, [order.id, status?.state, applyStatus]);

  async function run(kind: Work, action: (isCurrent: () => boolean) => Promise<void>) {
    if (disabled || workInProgress.current) return;
    workInProgress.current = true;
    const current = epoch.current;
    const isCurrent = () => epoch.current === current;
    setWork(kind);
    setError(null);
    onBusyRef.current?.(true);
    onWorkingRef.current?.(true);
    try {
      await action(isCurrent);
    } catch (err) {
      if (isCurrent()) setError(err instanceof Error ? err.message : "操作失败，请查询保存状态。");
    } finally {
      if (isCurrent()) {
        workInProgress.current = false;
        setWork(null);
        onWorkingRef.current?.(false);
      }
    }
  }

  function prepare() {
    void run("preparing", async (isCurrent) => {
      setNeedsStatusCheck(true);
      window.history.replaceState(window.history.state, "", savedCardReturnUrl(window.location.href, order.id));
      const next = await preparePlusSavedCard(order.id);
      if (!isCurrent()) return;
      if (!next.setup_intent_id || !next.client_secret || !next.publishable_key) {
        throw new Error("服务端未返回完整的卡片设置上下文，请查询保存状态。");
      }
      setSetup(next);
      const checked = await getPlusSavedCard(order.id);
      if (isCurrent()) applyStatus(checked);
    });
  }

  function checkStatus() {
    void run("checking", async (isCurrent) => {
      const next = await getPlusSavedCard(order.id);
      if (isCurrent()) applyStatus(next);
    });
  }

  function save() {
    const handle = mountedSetup.current;
    if (!handle || !ready || !canConfirm) return;
    if (!billing.name.trim() || !billing.line1.trim() || !billing.city.trim() || !/^[A-Z]{2}$/.test(billing.country)) {
      setError("请填写真实姓名、街道、城市和两位账单国家代码。");
      return;
    }
    if (["US", "CA"].includes(billing.country) && (!billing.state.trim() || !billing.postal_code.trim())) {
      setError("请填写账单地址的州或省和邮编。");
      return;
    }
    void run("saving", async (isCurrent) => {
      setNeedsStatusCheck(true);
      try {
        const out = await confirmSavedCardSetup({
          ...handle, billing, returnUrl: savedCardReturnUrl(window.location.href, order.id),
          readStatus: () => getPlusSavedCard(order.id),
        });
        if (!isCurrent()) return;
        applyStatus(out.status);
        if (out.confirmationError && !isSavedCard(out.status)) setError(out.confirmationError);
      } catch (err) {
        if (isCurrent() && !(err instanceof SavedCardStatusUnknownError)) setNeedsStatusCheck(false);
        throw err;
      }
    });
  }

  function refreshCheckout() {
    if (!isSavedCard(status)) return;
    void run("refreshing", async (isCurrent) => {
      const next = await refreshPlusSavedCardCheckout(order.id);
      if (!isCurrent()) return;
      if (next.order.id !== order.id || !next.order.checkout_url) throw new Error("服务端未返回当前订单的新结账链接。");
      setRefreshedUrl(next.order.checkout_url);
      onOrderRef.current(next.order);
      const checked = await getPlusSavedCard(order.id);
      if (isCurrent()) applyStatus(checked);
    });
  }

  const inactive = disabled || work !== null;
  const saved = isSavedCard(status);
  const canConfirm = Boolean(setup && ready && !saved && !needsStatusCheck
    && status && ["requires_payment_method", "requires_confirmation", "requires_action"].includes(status.state));
  const shownUrl = refreshedUrl || (status?.state === "checkout_ready" ? order.checkout_url : null);
  const changeBilling = (field: keyof BillingInput) => (event: React.ChangeEvent<HTMLInputElement>) => {
    const value = event.currentTarget.value;
    setBilling((previous) => ({ ...previous, [field]: field === "country" ? value.toUpperCase() : value }));
  };

  return (
    <Paper withBorder p="lg" radius="md" data-testid="plus-saved-card-panel"
      onKeyDownCapture={(event) => {
        if (event.key === "Enter" && event.target instanceof HTMLInputElement) {
          event.preventDefault();
          event.stopPropagation();
        }
      }}>
      <Stack gap="md">
        <Group justify="space-between">
          <Text fw={600}>Plus 保存付款方式</Text>
          <Badge color={saved ? "green" : "blue"} variant="light">{saved ? "已保存" : "设置付款方式"}</Badge>
        </Group>
        <Text size="sm">先保存付款方式，按银行提示完成验证；再点击「重新提链」刷新同账号的结账链接。</Text>
        <Text size="xs" c="dimmed">卡片通过 Stripe 安全输入框提交。此处仅保存付款方式，已保存，不代表已付款。</Text>
        <Alert color={saved ? "green" : "blue"} role="status">
          {statusLoading ? "正在读取保存状态…" : status ? STATUS_LABELS[status.state] : "保存状态尚未读取，请点击查询保存状态。"}
          {status?.message && <Text size="sm" mt={4}>{status.message}</Text>}
          {saved && status?.card && <Text size="sm" mt={4}>已保存卡片：{status.card.brand} ···· {status.card.last4}</Text>}
        </Alert>
        {!saved && (
          <Group>
            <Button type="button" variant="light" loading={work === "preparing"}
              disabled={inactive || statusLoading || needsStatusCheck || isSavedCardPending(status?.state)} onClick={prepare}>
              {setup ? "重新加载安全输入框" : "加载安全输入框"}
            </Button>
          </Group>
        )}
        {setup && !saved && (
          <Stack gap="sm">
            <Text size="sm" fw={500}>填写持卡人的真实账单信息</Text>
            <SimpleGrid cols={{ base: 1, xs: 2 }}>
              <TextInput label="持卡人姓名" value={billing.name} onChange={changeBilling("name")} disabled={inactive} autoComplete="cc-name" />
              <TextInput label="账单邮箱" type="email" value={billing.email || ""} onChange={changeBilling("email")} disabled={inactive} autoComplete="email" />
            </SimpleGrid>
            <TextInput label="街道地址" value={billing.line1} onChange={changeBilling("line1")} disabled={inactive} autoComplete="address-line1" />
            <SimpleGrid cols={{ base: 1, xs: 2 }}>
              <TextInput label="城市" value={billing.city} onChange={changeBilling("city")} disabled={inactive} autoComplete="address-level2" />
              <TextInput label="州 / 省" value={billing.state} onChange={changeBilling("state")} disabled={inactive} autoComplete="address-level1" />
              <TextInput label="邮编" value={billing.postal_code} onChange={changeBilling("postal_code")} disabled={inactive} autoComplete="postal-code" />
              <TextInput label="账单国家（两位代码）" value={billing.country} onChange={changeBilling("country")} maxLength={2} disabled={inactive} autoComplete="country" />
            </SimpleGrid>
            <div aria-disabled={inactive} style={inactive ? { pointerEvents: "none", opacity: 0.65 } : undefined}>
              <div id={targetId} data-testid="stripe-setup-element" />
            </div>
            {!ready && <Text size="xs" c="dimmed">正在加载 Stripe 安全输入框…</Text>}
            <Button type="button" disabled={inactive || !canConfirm} loading={work === "saving"} onClick={save}>保存付款方式并完成银行验证</Button>
          </Stack>
        )}
        {needsStatusCheck && <Text size="sm" c="orange">操作结果待核实。先查询保存状态，再继续操作。</Text>}
        <Group>
          <Button type="button" variant="default" loading={work === "checking"} disabled={inactive || statusLoading} onClick={checkStatus}>查询保存状态</Button>
          <Button type="button" loading={work === "refreshing"} disabled={inactive || !saved} onClick={refreshCheckout}>重新提链</Button>
        </Group>
        {shownUrl && <CheckoutLinkCard url={shownUrl} />}
        {shownUrl && onVerify && (
          <Button type="button" variant="light" loading={work === "verifying"} disabled={inactive}
            onClick={() => { void run("verifying", async () => { await onVerify(); }); }}>
            我已完成支付，查询订阅
          </Button>
        )}
        {error && <Alert color="red" role="alert">{error}</Alert>}
      </Stack>
    </Paper>
  );
}
