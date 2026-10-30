import { ActionIcon, Badge, Container, Group, Tabs, Text, Title, Tooltip, useMantineColorScheme } from "@mantine/core";
import { IconHistory, IconLogout, IconMoon, IconNetwork, IconReceipt, IconSun } from "@tabler/icons-react";
import { useCallback, useState } from "react";

import { loadHistory, recordOrder } from "./api/history";
import type { Order } from "./api/client";
import { HistoryTable } from "./components/HistoryTable";
import { ProxySettings } from "./components/ProxySettings";
import { OrderNew } from "./pages/OrderNew";

export function App() {
  const [history, setHistory] = useState(loadHistory);
  const { colorScheme, toggleColorScheme } = useMantineColorScheme();
  const record = useCallback((order: Order) => setHistory(recordOrder(order)), []);

  async function logout() {
    await fetch("/logout", { method: "POST" });
    window.location.href = "/login";
  }

  return (
    <Container size={1040} py="xl" className="studio-container">
      <Group justify="space-between" align="center" mb="lg">
        <div>
          <Title order={2}>gpt-subscribe-studio</Title>
          <Text size="sm" c="dimmed">
            保留协议模式与 Stripe 输入框模式；订单阶段和支付结果以服务端返回为准。
          </Text>
        </div>
        <Group gap="xs">
          <Tooltip label={colorScheme === "dark" ? "浅色模式" : "深色模式"}>
            <ActionIcon variant="default" size="lg" onClick={toggleColorScheme} aria-label="切换主题">
              {colorScheme === "dark" ? <IconSun size={18} /> : <IconMoon size={18} />}
            </ActionIcon>
          </Tooltip>
          <Tooltip label="退出登录">
            <ActionIcon variant="default" size="lg" onClick={() => void logout()} aria-label="退出">
              <IconLogout size={18} />
            </ActionIcon>
          </Tooltip>
        </Group>
      </Group>

      <Tabs defaultValue="order" keepMounted>
        <Tabs.List mb="lg">
          <Tabs.Tab value="order" leftSection={<IconReceipt size={16} />}>
            开通
          </Tabs.Tab>
          <Tabs.Tab
            value="history"
            leftSection={<IconHistory size={16} />}
            rightSection={history.length ? <Badge size="xs" variant="light">{history.length}</Badge> : null}
          >
            开通记录
          </Tabs.Tab>
          <Tabs.Tab value="proxy" leftSection={<IconNetwork size={16} />}>
            代理设置
          </Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="order">
          <OrderNew onRecord={record} />
        </Tabs.Panel>
        <Tabs.Panel value="history">
          <HistoryTable items={history} />
        </Tabs.Panel>
        <Tabs.Panel value="proxy">
          <ProxySettings />
        </Tabs.Panel>
      </Tabs>

      <Text size="xs" c="dimmed" ta="center" mt="xl">
        gpt-subscribe-studio
      </Text>
    </Container>
  );
}
