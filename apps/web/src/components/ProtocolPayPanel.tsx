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

import { payProtocol, type BillingInput, type ProtocolPayResult } from "../api/client";
import { randomBillingExample } from "../card-payment/billingExamples";

interface Props {
  orderId: string;
  /** 订单创建时选的地区，用于预填账单国家 */
  country: string;
  disabled?: boolean;
  onDone?: (result: ProtocolPayResult) => void;
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
  { title: "hCaptcha", description: "passive token（本地 solver 会话）" },
  { title: "payment_methods", description: "服务端向 Stripe 提交卡号，换 pm_" },
  { title: "confirm", description: "402 拒卡 / 200 challenge / 成功" },
  { title: "poll", description: "轮询终态" },
];

export function ProtocolPayPanel({ orderId, country, disabled, onDone }: Props) {
  const [card, setCard] = useState<CardDraft>(EMPTY_CARD);
  const [billing, setBilling] = useState<BillingInput>(() => {
    const base: BillingInput = {
      email: "", name: "", line1: "", city: "", state: "", postal_code: "",
      country: country || "US",
    };
    return randomBillingExample(base);
  });
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

    const number = card.number.replace(/[\s-]/g, "");
    if (!/^\d{12,19}$/.test(number)) return setError("卡号格式不对（12–19 位数字）。");
    if (!/^\d{3,4}$/.test(card.cvc)) return setError("CVC 格式不对（3–4 位）。");
    if (!/^\d{1,2}$/.test(card.exp_month)) return setError("到期月份填 MM。");
    if (!/^\d{2}$|^\d{4}$/.test(card.exp_year)) return setError("到期年份填 YY 或 YYYY。");
    if (!billing.name.trim() || !billing.line1.trim() || !billing.city.trim() || !billing.postal_code.trim()) {
      return setError("账单地址要填全：姓名、街道、城市、邮编。");
    }

    setBusy(true);
    try {
      const out = await payProtocol(orderId, {
        card: { number, cvc: card.cvc, exp_month: card.exp_month, exp_year: card.exp_year },
        billing: { ...billing, country: (billing.country || "US").toUpperCase() },
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

  return (
    <Stack gap="md">
      <Alert color="blue" variant="light" icon={<IconAlertCircle size={18} />} title="全协议模式（无浏览器）">
        服务端直接用 HTTP 和 Stripe 对话：不加载 Stripe.js、不弹 iframe、不需要你点验证框。
        卡号只在这一次请求里使用，服务端不写库、不打日志。
        单次耗时约 60–180 秒，中途别关页面。
      </Alert>

      <Paper withBorder p="md" radius="md">
        <Stack gap="sm">
          <Text fw={600}>卡片</Text>
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
            <Badge variant="light">{billing.country || "US"}</Badge>
          </Group>
          <Text size="xs" c="dimmed">
            账单国家必须和订单地区一致，否则 Stripe 会要求补验证。
          </Text>
          <SimpleGrid cols={{ base: 1, xs: 2 }}>
            <TextInput label="持卡人姓名" value={billing.name} onChange={changeBilling("name")} disabled={busy} />
            <TextInput label="邮箱" value={billing.email ?? ""} onChange={changeBilling("email")} disabled={busy} />
          </SimpleGrid>
          <TextInput label="街道地址" value={billing.line1} onChange={changeBilling("line1")} disabled={busy} />
          <SimpleGrid cols={{ base: 1, xs: 3 }}>
            <TextInput label="城市" value={billing.city} onChange={changeBilling("city")} disabled={busy} />
            <TextInput label="州 / 省" value={billing.state} onChange={changeBilling("state")} disabled={busy} />
            <TextInput label="邮编" value={billing.postal_code} onChange={changeBilling("postal_code")} disabled={busy} />
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

      <Group justify="flex-end">
        <Button size="md" loading={busy} disabled={disabled || busy} onClick={() => void run()}>
          {busy ? "协议支付中…" : "开始协议支付"}
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
