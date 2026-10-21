import { Alert, Anchor, Button, Group, Loader, Stack, Text, ThemeIcon, Title } from "@mantine/core";
import { IconCheck, IconX } from "@tabler/icons-react";

import { formatAmount, type Order } from "../api/client";
import { FAILED_STATUSES, SUCCESS_STATUSES } from "../api/history";
import { planLabel, regionLabel } from "../plans";
import { STATUS_LABELS } from "./OrderSummary";

interface Props {
  order: Order;
  onReset: () => void;
}

export function ResultPanel({ order, onReset }: Props) {
  const success = SUCCESS_STATUSES.includes(order.status);
  const failed = FAILED_STATUSES.includes(order.status);
  const amount = formatAmount(order.amount, order.currency);
  const color = success ? "green" : failed ? "red" : "yellow";
  const title = success
    ? "开通成功"
    : failed
      ? "开通失败"
      : (STATUS_LABELS[order.status] ?? order.status);

  return (
    <Stack gap="md" role="status" aria-live="polite">
      <Group align="center" wrap="nowrap">
        {success || failed ? (
          <ThemeIcon color={color} size={44} radius="xl" variant="light">
            {success ? <IconCheck size={26} /> : <IconX size={26} />}
          </ThemeIcon>
        ) : (
          <Loader size={44} color={color} />
        )}
        <div>
          <Title order={3}>{title}</Title>
          <Text size="sm" c="dimmed">
            {planLabel(order.plan)} · {regionLabel(order.billing_country)}
            {amount ? ` · ${amount}` : ""}
          </Text>
        </div>
      </Group>

      {order.subscription?.active && (
        <Alert color="green" variant="light" title="订阅已生效">
          {order.subscription.expires_at ? `到期时间 ${order.subscription.expires_at}` : "订阅已经生效。"}
        </Alert>
      )}
      {(order.next_action?.url || order.next_action?.checkout_url || (order.plan === "team" && order.checkout_url && !success && !failed)) && (
        <Alert color="yellow" variant="light" title={order.next_action?.challenge_type === "intent_confirmation_challenge" ? "需要完成 hCaptcha 人机验证" : "支付验证与状态确认"}>
          <Anchor href={order.next_action?.url || order.next_action?.checkout_url || order.checkout_url || "#"} target="_blank" rel="noreferrer">
            {order.next_action?.url ? "打开支付验证页面" : "打开官方结账页完成验证"}
          </Anchor>
          {order.plan === "team" && <Text size="xs">页面会自动查询 Stripe Hosted 支付结果，请勿重复提交付款。</Text>}
        </Alert>
      )}
      {order.last_error && !success && (
        <Alert color="red" variant="light" title="失败原因">
          {order.last_error}
        </Alert>
      )}

      {(success || failed) && (
        <Group>
          <Button onClick={onReset}>再开通一单</Button>
        </Group>
      )}
    </Stack>
  );
}
