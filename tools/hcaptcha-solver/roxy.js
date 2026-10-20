/**
 * RoxyBrowser 本地 API 客户端（只保留 solver 需要的部分）。
 *
 * 官方文档：https://faq.roxybrowser.com/api-documentation/api-reference.html
 * 默认 API host: http://127.0.0.1:50000
 * 认证：header 里带 token
 */
const BASE_HOST = process.env.ROXY_HOST || '127.0.0.1';
const BASE_PORT = Number(process.env.ROXY_PORT || 50000);
const TOKEN = process.env.ROXY_TOKEN || '';

function baseUrl() {
  return `http://${BASE_HOST}:${BASE_PORT}`;
}

async function call(method, path, body) {
  const url = baseUrl() + path;
  const headers = { 'Content-Type': 'application/json' };
  if (TOKEN) headers.token = TOKEN;
  const opts = { method, headers };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(url, opts);
  const text = await res.text();
  let json;
  try {
    json = JSON.parse(text);
  } catch (_) {
    throw new Error(`Roxy ${path} 非 JSON 响应: ${text.slice(0, 200)}`);
  }
  return json;
}

const roxy = {
  baseUrl,

  /** 健康检查 */
  async health() {
    return call('GET', '/health');
  },

  /**
   * 列出所有浏览器配置文件。
   * 返回结构里的字段名随版本略有差异，这里做兼容处理。
   */
  async listProfiles() {
    const r = await call('POST', '/browser/list', { page: 1, pageSize: 100 });
    const data = r.data;
    if (Array.isArray(data)) return data;
    return (data && (data.list || data.rows || data.data)) || [];
  },

  /** 打开一个 profile，返回 { ws, ... } */
  async openProfile(dirId, args) {
    const r = await call('POST', '/browser/open', { dirId, args: args || [] });
    if (r.code !== 0) {
      const err = new Error(`open failed: ${r.msg || JSON.stringify(r)}`);
      err.code = r.code;
      err.msg = r.msg;
      throw err;
    }
    return r.data || {};
  },

  /** 关闭 profile */
  async closeProfile(dirId) {
    return call('POST', '/browser/close', { dirId });
  },

  /** 当前已打开的 profile 进程信息 */
  async connectionInfo() {
    return call('GET', '/browser/connection_info');
  },

  /**
   * 确保有一个可用的 profile：
   * 优先用 ROXY_DIR_ID，否则取列表里第一个。
   */
  async ensureProfile() {
    const want = process.env.ROXY_DIR_ID;
    if (want) return want;
    const list = await roxy.listProfiles();
    if (!list.length) {
      throw new Error('RoxyBrowser 里没有任何 profile，请先在 GUI 里建一个');
    }
    const first = list[0];
    const id = first.dirId || first.dir_id || first.id;
    if (!id) throw new Error('无法从 profile 列表里识别 dirId: ' + JSON.stringify(first).slice(0, 200));
    return id;
  },
};

module.exports = roxy;
