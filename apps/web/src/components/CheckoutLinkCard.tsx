import { ActionIcon, Anchor, Button, Code, CopyButton, Group, Paper, Stack, Text, Tooltip } from "@mantine/core";
import { IconCheck, IconCopy, IconExternalLink, IconLink } from "@tabler/icons-react";

interface Props {
  url?: string | null;
  /** 已有订单后出现；步进到结果页也保留，方便回头查 */
  visible?: boolean;
}

/**
 * 结账长链常驻卡片。
 * 生成之后无论切到哪个支付模式、走到第几步，都保留可查看 + 可复制的入口。
 */
export function CheckoutLinkCard({ url, visible = true }: Props) {
  if (!visible || !url) return null;
  const short = url.replace(/^https:\/\//, "");

  return (
    <Paper withBorder p="md" radius="md" data-testid="checkout-link-card">
      <Stack gap="xs">
        <Group justify="space-between" align="center" wrap="nowrap">
          <Group gap={6} wrap="nowrap">
            <IconLink size={16} />
            <Text fw={600} size="sm">结账长链</Text>
          </Group>
          <Group gap={4} wrap="nowrap">
            <Tooltip label="在新标签页打开">
              <ActionIcon component="a" href={url} target="_blank" rel="noreferrer"
                variant="light" size="md" aria-label="打开长链">
                <IconExternalLink size={16} />
              </ActionIcon>
            </Tooltip>
            <CopyButton value={url} timeout={1600}>
              {({ copied, copy }) => (
                <Tooltip label={copied ? "已复制" : "复制到剪贴板"}>
                  <ActionIcon onClick={copy} variant={copied ? "filled" : "light"}
                    color={copied ? "teal" : undefined} size="md" aria-label="复制长链">
                    {copied ? <IconCheck size={16} /> : <IconCopy size={16} />}
                  </ActionIcon>
                </Tooltip>
              )}
            </CopyButton>
          </Group>
        </Group>

        <Code block style={{ wordBreak: "break-all", whiteSpace: "pre-wrap", fontSize: 11 }}>
          {short}
        </Code>

        <Group gap="xs">
          <CopyButton value={url} timeout={1600}>
            {({ copied, copy }) => (
              <Button size="xs" variant={copied ? "filled" : "default"}
                color={copied ? "teal" : undefined} onClick={copy}
                leftSection={copied ? <IconCheck size={14} /> : <IconCopy size={14} />}>
                {copied ? "已复制整条链接" : "复制整条链接"}
              </Button>
            )}
          </CopyButton>
          <Button component="a" href={url} target="_blank" rel="noreferrer"
            size="xs" variant="default" leftSection={<IconExternalLink size={14} />}>
            打开长链
          </Button>
          <Anchor href={url} target="_blank" rel="noreferrer" size="xs" c="dimmed" visibleFrom="sm">
            官方结账页付款后回本页查订阅
          </Anchor>
        </Group>
      </Stack>
    </Paper>
  );
}
