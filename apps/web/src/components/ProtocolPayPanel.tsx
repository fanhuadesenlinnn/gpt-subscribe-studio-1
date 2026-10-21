import {
  Alert,
  Badge,
  Button,
  Code,
  Group,
  Paper,
  PasswordInput,
  SimpleGrid,
  Stack,
  Text,
  Textarea,
  TextInput,
  Timeline,
} from "@mantine/core";
import { IconAlertCircle, IconCheck, IconClock, IconX } from "@tabler/icons-react";
import { useState } from "react";

import { CheckoutLinkCard } from "./CheckoutLinkCard";

import { formatAmount, payProtocol, type BillingInput, type ProtocolPayResult } from "../api/client";
import { FIXED_TAX_FREE_BILLING, FIXED_TAX_FREE_BILLING_LABEL } from "../card-payment/billingExamples";

interface Props {
  orderId: string;
  /** 订单创建时选的地区，用于预填账单国家 */
  country: string;
  /** 应付金额（最小货币单位）与币种，来自 /team/quote */
  amount?: string | null;
  currency?: string | null;
  quotationError?: string | null;
  /** 本轮实际使用的出口（只显示国家与 ISP，不显示账号密码） */
  exitLabel?: string;
  /** Stripe 公钥；有它才能挂载 Stripe 输入框 */
  publishableKey?: string | null;
  checkoutUrl?: string | null;
  disabled?: boolean;
  onDone?: (result: ProtocolPayResult) => void;
  /** 重新向 Stripe 读一次金额（报价超时会用） */
  onRefreshQuote?: () => Promise<void> | void;
}


interface CardDraft {
  number: string;
  exp_month: string;
  exp_year: string;
  cvc: string;
}

const EMPTY_CARD: CardDraft = { number: "", exp_month: "", exp_year: "", cvc: "" };

/** 结果分类：把服务端 state 翻成人话，并标出下一步该干什么。 */
function describe(result: ProtocolPayResult) {
  switch (result.state) {
    case "paid":
      return {
        color: "green" as const,
        icon: <IconCheck size={18} />,
        title: "支付成功",
        hint: `PaymentIntent 状态 ${result.intent_status ?? "succeeded"}`,
      };
    case "card_declined":
      return {
        color: "red" as const,
        icon: <IconX size={18} />,
        title: "卡片被拒",
        hint: [
          result.code && `code=${result.code}`,
          result.decline_code && `decline_code=${result.decline_code}`,
          result.charge_id && `charge=${result.charge_id}`,
          result.message,
        ].filter(Boolean).join(" · "),
      };
    case "captcha_rejected":
      return {
        color: "orange" as const,
        icon: <IconAlertCircle size={18} />,
        title: "hCaptcha 验证未通过",
        hint: [
          result.code && `code=${result.code}`,
          result.message,
          "Stripe 要求二次人机验证，当前出口 IP 未通过评分；换代理 SID 再试。",
        ].filter(Boolean).join(" · "),
      };
    case "challenge_required":
      return {
        color: "yellow" as const,
        icon: <IconClock size={18} />,
        title: "需要完成 3DS 验证",
        hint: `PaymentIntent 状态 ${result.intent_status ?? "requires_action"}`,
      };
    default:
      return {
        color: "gray" as const,
        icon: <IconClock size={18} />,
        title: `未知结果：${result.state}`,
        hint: [result.code, result.decline_code, result.message].filter(Boolean).join(" · "),
      };
  }
}

const STEPS = [
  { title: "指纹注册", description: "m.stripe.com/6 × 4 → guid/muid/sid" },
  { title: "init", description: "取金额、hCaptcha sitekey 与 rqdata" },
  { title: "elements / 地址", description: "elements/sessions + tax_region" },
  { title: "confirm", description: "402 拒卡 / 200 challenge / 成功" },
  { title: "poll", description: "轮询终态" },
];

export function ProtocolPayPanel({
  orderId, country, amount, currency, quotationError, exitLabel,
  checkoutUrl, disabled, onDone, onRefreshQuote,
}: Props) {
  const [card, setCard] = useState<CardDraft>(EMPTY_CARD);
  // 账单地址钉死在美国免税州：税额恒定 → 会话总额不变 →
  // confirm 的 expected_amount 不会因地址变动而对不上。
  const [billing, setBilling] = useState<BillingInput>(() => ({
    email: "",
    name: "",
    ...FIXED_TAX_FREE_BILLING,
  }));
  const [proxy, setProxy] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ProtocolPayResult | null>(null);

  const changeCard = (field: keyof CardDraft) => (event: React.ChangeEvent<HTMLInputElement>) =>
    setCard({ ...card, [field]: event.currentTarget.value });
  const changeBilling = (field: keyof BillingInput) => (event: React.ChangeEvent<HTMLInputElement>) =>
    setBilling({ ...billing, [field]: event.currentTarget.value });

  async function run() {
    setError(null);
    setResult(null);

    if (!billing.name.trim() || !billing.line1.trim() || !billing.city.trim() || !billing.postal_code.trim()) {
      return setError("账单地址要填全：姓名、街道、城市、邮编。");
    }
    if (!(billing.email ?? "").trim()) {
      return setError("邮箱必须填写：Stripe 会把收据发到这个地址，缺失会影响风控判定。");
    }
    if (billing.country === "US" || billing.country === "CA") {
      if (!billing.state.trim()) return setError(`${billing.country} 账单地址必须填州/省。`);
    }

    const payloadBilling = { ...billing, country: (billing.country || "US").toUpperCase() };

    const number = card.number.replace(/[\s-]/g, "");
    if (!/^\d{12,19}$/.test(number)) return setError("卡号格式不对（12–19 位数字）。");
    if (!/^\d{3,4}$/.test(card.cvc)) return setError("CVC 格式不对（3–4 位）。");
    if (!/^\d{1,2}$/.test(card.exp_month)) return setError("到期月份填 MM。");
    if (!/^\d{2}$|^\d{4}$/.test(card.exp_year)) return setError("到期年份填 YY 或 YYYY。");

    setBusy(true);
    try {
      const out = await payProtocol(orderId, {
        card: { number, cvc: card.cvc, exp_month: card.exp_month, exp_year: card.exp_year },
        billing: payloadBilling,
        proxy: proxy.trim() || undefined,
      });
      setResult(out);
      onDone?.(out);
    } catch (err) {
      setError(err instanceof Error ? err.message : "协议支付失败");
    } finally {
      setBusy(false);
    }
  }

  const view = result ? describe(result) : null;

  const price = formatAmount(amount, currency);

  return (
    <Stack gap="md" maw={720} mx="auto">
      {/* 金额放最上面：确认支付之前先让人看清要付多少 */}
      <Paper withBorder p="lg" radius="md" className="protocol-amount">
        <Group justify="space-between" align="flex-end" wrap="nowrap">
          <div>
            <Text size="xs" c="dimmed" tt="uppercase" fw={600}>应付金额</Text>
            <Text fz={32} fw={700} lh={1.1} mt={4}>
              {price ?? (quotationError ? "读取失败" : "读取中…")}
            </Text>
            <Text size="xs" c="dimmed" mt={4}>
              {country}
              {currency ? ` · ${currency.toUpperCase()}` : ""}
              {exitLabel ? ` · 出口 ${exitLabel}` : ""}
            </Text>
          </div>
          <Badge size="lg" variant="light" color={price ? "green" : "gray"}>
            {price ? "待支付" : "待报价"}
          </Badge>
        </Group>
        {quotationError && (
          <Alert color="red" variant="light" mt="sm" icon={<IconAlertCircle size={16} />}>
            金额读取失败：{quotationError}
          </Alert>
        )}
        {!price && !quotationError && (
          <Group justify="space-between" align="center" mt="sm" wrap="nowrap">
            <Text size="xs" c="dimmed">
              正在向 Stripe 读取应付金额（init + tax_region），读到后按钮才可用。若一直不动，点右侧刷新。
            </Text>
            {onRefreshQuote && (
              <Button size="xs" variant="light" disabled={busy} onClick={() => void onRefreshQuote()}>
                刷新金额
              </Button>
            )}
          </Group>
        )}
        {quotationError && onRefreshQuote && (
          <Button size="xs" variant="light" mt="xs" disabled={busy} onClick={() => void onRefreshQuote()}>
            重新读取金额
          </Button>
        )}
      </Paper>

      <CheckoutLinkCard url={checkoutUrl} />

      <Alert color="blue" variant="light" icon={<IconAlertCircle size={18} />} title="协议链路">
        服务端按真实浏览器时序跑完：
        指纹注册 → init → elements/sessions → tax_region → hCaptcha → confirm → poll。
        卡号只在本次请求里使用，不落库、不打日志。耗时约 60–180 秒，中途别关页面。
      </Alert>

      <Paper withBorder p="md" radius="md">
        <Stack gap="sm">
            <TextInput label="卡号" value={card.number} onChange={changeCard("number")}
              inputMode="numeric" autoComplete="off" disabled={busy} placeholder="4242424242424242" />
            <SimpleGrid cols={{ base: 1, xs: 3 }}>
              <TextInput label="到期月份" placeholder="MM" value={card.exp_month}
                onChange={changeCard("exp_month")} inputMode="numeric" maxLength={2} disabled={busy} />
              <TextInput label="到期年份" placeholder="YYYY" value={card.exp_year}
                onChange={changeCard("exp_year")} inputMode="numeric" maxLength={4} disabled={busy} />
              <PasswordInput label="CVC" value={card.cvc} onChange={changeCard("cvc")}
                inputMode="numeric" maxLength={4} disabled={busy} />
            </SimpleGrid>

        </Stack>
      </Paper>

      <Paper withBorder p="md" radius="md">
        <Stack gap="sm">
          <Group justify="space-between">
            <Text fw={600}>账单地址</Text>
            <Badge variant="light">US / {billing.state}</Badge>
          </Group>
          <Text size="xs" c="dimmed">
            {FIXED_TAX_FREE_BILLING_LABEL}。全州无销售税，应付金额不会因地址改动而变动。
          </Text>
          {country && country.toUpperCase() !== "US" && (
            <Alert color="orange" variant="light" icon={<IconAlertCircle size={16} />}>
              订单地区是 {country.toUpperCase()}，但账单地址固定为美国免税州。
              出口会按账单国家对齐到 US。要跑其他国家请新建订单时选择对应地区。
            </Alert>
          )}
          <SimpleGrid cols={{ base: 1, xs: 2 }}>
            <TextInput label="持卡人姓名" value={billing.name} onChange={changeBilling("name")} disabled={busy} />
            <TextInput label="邮箱" value={billing.email ?? ""} onChange={changeBilling("email")} disabled={busy} />
          </SimpleGrid>
          <TextInput label="街道地址" value={billing.line1} readOnly disabled />
          <SimpleGrid cols={{ base: 1, xs: 3 }}>
            <TextInput label="城市" value={billing.city} readOnly disabled />
            <TextInput label="州 / 省" value={billing.state} readOnly disabled />
            <TextInput label="邮编" value={billing.postal_code} readOnly disabled />
          </SimpleGrid>
        </Stack>
      </Paper>

      <Textarea
        label="指定代理（可选）"
        description="留空则用「代理设置」里 checkout 的网关，并按账单国家自动对齐地区。"
        placeholder="http://user:pass@host:3010"
        value={proxy}
        onChange={(event) => setProxy(event.currentTarget.value)}
        autosize
        minRows={1}
        maxRows={2}
        disabled={busy}
      />

      <Group justify="space-between" align="center">
        <Text size="xs" c="dimmed">
          {price
            ? `确认支付 ${price}；币种与税额以 Stripe 计算为准。`
            : "金额尚未读到，按钮暂不可用。"}
        </Text>
        <Button
          size="md"
          loading={busy}
          disabled={disabled || busy || !price}
          onClick={() => void run()}
        >
          {busy ? "协议支付中…" : !price ? "等待报价" : `确认支付 ${price}`}
        </Button>
      </Group>

      {busy && (
        <Paper withBorder p="md" radius="md">
          <Text size="sm" fw={600} mb="xs">服务端正在跑这条链路</Text>
          <Timeline active={3} bulletSize={18} lineWidth={2}>
            {STEPS.map((step) => (
              <Timeline.Item key={step.title} title={step.title}>
                <Text size="xs" c="dimmed">{step.description}</Text>
              </Timeline.Item>
            ))}
          </Timeline>
        </Paper>
      )}

      {error && (
        <Alert color="red" variant="light" icon={<IconAlertCircle size={18} />} title="请求失败" role="alert">
          {error}
        </Alert>
      )}

      {result && view && (
        <Alert color={view.color} variant="light" icon={view.icon} title={view.title} role="status">
          <Stack gap={6}>
            <Text size="sm">{view.hint}</Text>
            <Code block style={{ fontSize: 12 }}>
              {JSON.stringify(result, null, 2)}
            </Code>
          </Stack>
        </Alert>
      )}
    </Stack>
  );
}
