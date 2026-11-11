import { preferences } from '@kit.ArkData';
import { common } from '@kit.AbilityKit';
import deviceInfo from '@ohos.deviceInfo';

/** 历史电脑条目 */
export interface PcHistory {
  base: string;
  name: string;
  ts: number;
}

/**
 * 连接信息 + 历史电脑列表 + 消息进度（preferences 持久化）
 */
export class Prefs {
  private static p: preferences.Preferences | null = null;

  static async init(ctx: common.UIAbilityContext): Promise<void> {
    if (Prefs.p === null) {
      Prefs.p = await preferences.getPreferences(ctx, 'yima');
    }
  }

  private static sp(): preferences.Preferences {
    if (Prefs.p === null) {
      throw new Error('Prefs 未初始化');
    }
    return Prefs.p;
  }

  /** 当前连接的电脑 base，如 http://192.168.1.5:8000；空=未连接 */
  static getBase(): string {
    try {
      return Prefs.sp().getSync('base', '') as string;
    } catch (e) {
      return '';
    }
  }

  static setBase(base: string): void {
    try {
      const p = Prefs.sp();
      p.putSync('base', base);
      p.flushSync();
    } catch (e) {
      // 忽略
    }
  }

  static clearBase(): void {
    try {
      const p = Prefs.sp();
      p.deleteSync('base');
      p.flushSync();
    } catch (e) {
      // 忽略
    }
  }

  /** 已收到的推送文字最大 id（增量轮询用） */
  static lastMsgId(): number {
    try {
      return Prefs.sp().getSync('lastMsgId', 0) as number;
    } catch (e) {
      return 0;
    }
  }

  static setLastMsgId(id: number): void {
    try {
      const p = Prefs.sp();
      p.putSync('lastMsgId', id);
      p.flushSync();
    } catch (e) {
      // 忽略
    }
  }

  /** 历史电脑列表（去重，最多 8 台） */
  static history(): PcHistory[] {
    try {
      const raw = Prefs.sp().getSync('history', '[]') as string;
      const arr = JSON.parse(raw) as PcHistory[];
      if (arr instanceof Array) {
        return arr;
      }
    } catch (e) {
      // 忽略
    }
    return [];
  }

  static addHistory(base: string, name: string): void {
    const arr = Prefs.history();
    const out: PcHistory[] = [];
    out.push({ base: base, name: name, ts: Date.now() });
    for (let i = 0; i < arr.length; i++) {
      if (arr[i].base !== base && out.length < 8) {
        out.push(arr[i]);
      }
    }
    try {
      const p = Prefs.sp();
      p.putSync('history', JSON.stringify(out));
      p.flushSync();
    } catch (e) {
      // 忽略
    }
  }

  static removeHistory(base: string): void {
    const arr = Prefs.history();
    const out: PcHistory[] = [];
    for (let i = 0; i < arr.length; i++) {
      if (arr[i].base !== base) {
        out.push(arr[i]);
      }
    }
    try {
      const p = Prefs.sp();
      p.putSync('history', JSON.stringify(out));
      p.flushSync();
    } catch (e) {
      // 忽略
    }
  }

  /**
   * 本机设备身份（多手机场景下电脑端定向推送用）。
   * did 首次随机生成并持久化；name 默认取设备型号，回退「手机-XXXX」。
   */
  static deviceId(): string {
    try {
      const p = Prefs.sp();
      let did = p.getSync('deviceId', '') as string;
      if (did === '') {
        did = Math.floor(Math.random() * 0xffffffff).toString(16).padStart(8, '0');
        p.putSync('deviceId', did);
        p.flushSync();
      }
      return did;
    } catch (e) {
      return '00000000';
    }
  }

  static deviceName(): string {
    let name = '';
    try {
      name = deviceInfo.marketName !== undefined ? deviceInfo.marketName : '';
    } catch (e) {
      name = '';
    }
    if (name === '') {
      try {
        name = deviceInfo.productModel !== undefined ? deviceInfo.productModel : '';
      } catch (e) {
        name = '';
      }
    }
    if (name === '') {
      name = '手机-' + Prefs.deviceId();
    }
    return name.substring(0, 24);
  }
}
