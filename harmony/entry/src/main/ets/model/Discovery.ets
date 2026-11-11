import { socket } from '@kit.NetworkKit';
import { util } from '@kit.ArkTS';

/**
 * 局域网电脑自动发现。
 *
 * 电脑端（app.py / yima_win.py）每 3 秒向 UDP 58887 广播：
 *   {"app":"LanFileTransfer","id":...,"name":主机名,"port":8000}
 * 手机端只需监听该端口即可拿到电脑地址，无需发送探测包。
 */

/** 发现的电脑 */
export interface PcBeacon {
  /** 可直接用于连接的 base，如 http://192.168.1.37:8000 */
  base: string;
  /** 电脑主机名 */
  name: string;
  /** 电脑 IP */
  ip: string;
}

const DISCOVERY_PORT: number = 58887;

let udp: socket.UDPSocket | null = null;
let handler: (b: PcBeacon) => void = (_b: PcBeacon) => {
};

/** 发现监听是否正在运行 */
export function discoveryRunning(): boolean {
  return udp !== null;
}

/**
 * 开始监听局域网广播（重复调用只保留一份）。
 * 返回 false 表示端口绑定失败（如被占用），调用方应回退到手动输入。
 */
export async function startDiscovery(onBeacon: (b: PcBeacon) => void): Promise<boolean> {
  if (udp !== null) {
    handler = onBeacon;
    return true;
  }
  const s: socket.UDPSocket = socket.constructUDPSocketInstance();
  try {
    await s.bind({ address: '0.0.0.0', port: DISCOVERY_PORT } as socket.NetAddress);
  } catch (e) {
    return false;
  }
  const decoder: util.TextDecoder = util.TextDecoder.create('utf-8');
  s.on('message', (info: socket.SocketMessageInfo) => {
    try {
      const text: string = decoder.decodeToString(new Uint8Array(info.message));
      const obj: Record<string, Object> = JSON.parse(text) as Record<string, Object>;
      if (obj['app'] !== 'LanFileTransfer') {
        return;
      }
      const port: number = Number(obj['port']);
      const ip: string = info.remoteInfo.address;
      const name: string = obj['name'] === undefined ? '电脑' : String(obj['name']);
      if (!(port > 0 && port < 65536) || ip === '') {
        return;
      }
      const beacon: PcBeacon = { base: 'http://' + ip + ':' + port, name: name, ip: ip };
      handler(beacon);
    } catch (e) {
      // 非本协议的包，忽略
    }
  });
  udp = s;
  handler = onBeacon;
  return true;
}

/** 停止监听并释放端口 */
export async function stopDiscovery(): Promise<void> {
  const s: socket.UDPSocket | null = udp;
  udp = null;
  if (s === null) {
    return;
  }
  try {
    s.off('message');
  } catch (e) {
    // 忽略
  }
  try {
    await s.close();
  } catch (e) {
    // 忽略
  }
}
