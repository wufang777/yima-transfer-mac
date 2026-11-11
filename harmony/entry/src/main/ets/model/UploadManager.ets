import { fileIo as fs } from '@kit.CoreFileKit';
import { common } from '@kit.AbilityKit';
import { backgroundTaskManager } from '@kit.BackgroundTasksKit';
import { notificationManager } from '@kit.NotificationKit';
import { CHUNK_SIZE, uploadInit, uploadChunk, uploadComplete } from './Api';

/**
 * 上传行数据（UI 渲染用，state：
 * queued 排队中 / sending 传输中 / done 已完成 / instant 秒传 / canceled 已取消 / failed 中断）
 */
export class UploadRowData {
  uri: string;
  name: string;
  size: number;
  sent: number;
  state: string;

  constructor(uri: string, name: string, size: number) {
    this.uri = uri;
    this.name = name;
    this.size = size;
    this.sent = 0;
    this.state = 'queued';
  }
}

export type UpdateFn = (rows: UploadRowData[]) => void;

function sleep(ms: number): Promise<void> {
  return new Promise<void>((resolve: () => void) => {
    setTimeout(() => {
      resolve();
    }, ms);
  });
}

/**
 * 全局单例上传管理器：
 * - 队列串行上传（多选文件不打架）
 * - 2MB 分片 + 断点续传 + 每片重试 4 次
 * - 可取消（分片边界停下、已传保留）、可重试（断点续传）
 * - 后台常驻任务（dataTransfer）锁屏不中断，通知栏显示进度
 */
export class UploadManager {
  private static inst: UploadManager | null = null;

  static instance(): UploadManager {
    if (UploadManager.inst === null) {
      UploadManager.inst = new UploadManager();
    }
    return UploadManager.inst;
  }

  private rows: UploadRowData[] = [];
  private canceled: Set<string> = new Set<string>();
  private running: boolean = false;
  private bgRunning: boolean = false;
  private notifReady: boolean = false;
  private ctx: common.UIAbilityContext | null = null;
  onUpdate: UpdateFn | null = null;

  private constructor() {
  }

  setContext(ctx: common.UIAbilityContext): void {
    this.ctx = ctx;
  }

  /** UI 订阅刷新（传回行的浅拷贝数组，直接喂给 @State） */
  private notify(): void {
    if (this.onUpdate !== null) {
      this.onUpdate(this.rows.slice());
    }
  }

  currentRows(): UploadRowData[] {
    return this.rows.slice();
  }

  /** 入队：外部把文件 uri 交给管理器 */
  enqueue(uri: string, name: string, size: number): void {
    // 同 uri 去重
    for (let i = 0; i < this.rows.length; i++) {
      if (this.rows[i].uri === uri) {
        this.rows[i].state = 'queued';
        this.notify();
        this.pump();
        return;
      }
    }
    this.rows.push(new UploadRowData(uri, name, size));
    this.notify();
    this.pump();
  }

  /** 取消某个文件（排队中直接取消，传输中在分片边界停下） */
  cancel(uri: string): void {
    this.canceled.add(uri);
    for (let i = 0; i < this.rows.length; i++) {
      if (this.rows[i].uri === uri && this.rows[i].state === 'queued') {
        this.rows[i].state = 'canceled';
        this.notify();
      }
    }
  }

  /** 重试（从断点续传） */
  retry(uri: string): void {
    for (let i = 0; i < this.rows.length; i++) {
      if (this.rows[i].uri === uri) {
        this.rows[i].state = 'queued';
        this.notify();
        this.pump();
        return;
      }
    }
  }

  private isCanceled(uri: string): boolean {
    return this.canceled.has(uri);
  }

  private hasPending(): boolean {
    for (let i = 0; i < this.rows.length; i++) {
      if (this.rows[i].state === 'queued') {
        return true;
      }
    }
    return false;
  }

  private async startBg(): Promise<void> {
    if (this.bgRunning || this.ctx === null) {
      return;
    }
    try {
      const req = new backgroundTaskManager.ContinuousTaskRequest();
      req.backgroundTaskModes = [backgroundTaskManager.BackgroundTaskMode.MODE_DATA_TRANSFER];
      await backgroundTaskManager.startBackgroundRunning(this.ctx, req);
      this.bgRunning = true;
    } catch (e) {
      // 后台任务申请失败不阻塞上传（退化为前台传输）
    }
  }

  private async stopBg(): Promise<void> {
    if (!this.bgRunning || this.ctx === null) {
      return;
    }
    try {
      await backgroundTaskManager.stopBackgroundRunning(this.ctx);
    } catch (e) {
      // 忽略
    }
    this.bgRunning = false;
  }

  private async ensureNotif(): Promise<void> {
    if (this.notifReady || this.ctx === null) {
      return;
    }
    this.notifReady = true;
    try {
      await notificationManager.requestEnableNotification(this.ctx);
    } catch (e) {
      // 用户拒绝授权则静默跳过通知
    }
  }

  private async note(title: string, text: string, progress: number): Promise<void> {
    try {
      const req: notificationManager.NotificationRequest = {
        id: 100,
        content: {
          notificationContentType: notificationManager.ContentType.NOTIFICATION_CONTENT_BASIC_TEXT,
          normal: {
            title: title,
            text: text,
            additionalText: progress >= 0 ? (progress + '%') : ''
          }
        }
      };
      await notificationManager.publish(req);
    } catch (e) {
      // 忽略通知失败
    }
  }

  /** 单工作协程串行消费队列 */
  private pump(): void {
    if (this.running) {
      return;
    }
    this.running = true;
    this.worker();
  }

  private async worker(): Promise<void> {
    await this.startBg();
    this.ensureNotif();
    while (this.hasPending()) {
      let job: UploadRowData | null = null;
      for (let i = 0; i < this.rows.length; i++) {
        if (this.rows[i].state === 'queued') {
          job = this.rows[i];
          break;
        }
      }
      if (job === null) {
        break;
      }
      if (this.isCanceled(job.uri)) {
        job.state = 'canceled';
        this.canceled.delete(job.uri);
        this.notify();
        continue;
      }
      await this.doUpload(job);
      if (this.isCanceled(job.uri)) {
        this.canceled.delete(job.uri);
      }
    }
    this.running = false;
    if (!this.hasPending()) {
      await this.stopBg();
    }
  }

  private setState(row: UploadRowData, state: string, sent: number): void {
    row.state = state;
    if (sent >= 0) {
      row.sent = sent;
    }
    this.notify();
  }

  private async doUpload(job: UploadRowData): Promise<void> {
    const b = this.baseUrl;
    if (b === '') {
      this.setState(job, 'failed', job.sent);
      await this.note('发送「' + job.name + '」', '连接已断开', -1);
      return;
    }
    let file: fs.File | null = null;
    try {
      file = fs.openSync(job.uri, fs.OpenMode.READ_ONLY);
      const st = fs.statSync(file.fd);
      if (job.size <= 0) {
        job.size = st.size;
      }
      const size = job.size;
      if (size <= 0) {
        throw new Error('无法获取文件大小');
      }

      // 1) init：秒传 / 断点
      const init = await uploadInit(b, job.name, size);
      if (init.ok === false) {
        throw new Error(init.error ? init.error : '初始化失败');
      }
      if (init.done === true) {
        this.setState(job, 'instant', size);
        await this.note('发送「' + job.name + '」', '已完成（电脑上已有同文件）', 100);
        fs.closeSync(file);
        return;
      }
      let offset = init.offset !== undefined ? init.offset : 0;
      const id = init.id !== undefined ? init.id : '';
      this.setState(job, 'sending', offset);

      // 2) 逐片上传
      const buf = new ArrayBuffer(CHUNK_SIZE);
      while (offset < size) {
        if (this.isCanceled(job.uri)) {
          this.setState(job, 'canceled', offset);
          await this.note('发送「' + job.name + '」', '已取消（重试可续传）', -1);
          fs.closeSync(file);
          return;
        }
        const want = Math.min(CHUNK_SIZE, size - offset);
        const rd = fs.readSync(file.fd, buf, { offset: offset, length: want });
        if (rd <= 0) {
          throw new Error('文件读取中断');
        }
        let ok = false;
        let lastErr: Error | null = null;
        for (let attempt = 0; attempt < 4; attempt++) {
          try {
            offset = await uploadChunk(b, id, offset, buf, rd);
            ok = true;
            break;
          } catch (e) {
            lastErr = e instanceof Error ? e : new Error(String(e));
            await this.note('发送「' + job.name + '」', '网络波动，重试中…', -1);
            await sleep(800 * (attempt + 1));
            // 对齐服务端权威进度
            try {
              const st2 = await uploadInit(b, job.name, size);
              if (st2.ok === true && st2.offset !== undefined) {
                offset = st2.offset;
              }
            } catch (e2) {
              // 忽略
            }
          }
        }
        if (!ok) {
          throw (lastErr !== null ? lastErr : new Error('分片上传失败'));
        }
        const pct = Math.floor(offset * 100 / size);
        await this.note('发送「' + job.name + '」', fmtSize(offset) + ' / ' + fmtSize(size), pct);
        this.setState(job, 'sending', offset);
      }

      // 3) complete
      const fin = await uploadComplete(b, id);
      if (fin.ok !== true) {
        throw new Error(fin.error ? fin.error : '完成校验失败');
      }
      this.setState(job, 'done', size);
      await this.note('发送「' + job.name + '」', '已完成 · ' + fmtSize(size), 100);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      this.setState(job, 'failed', job.sent);
      await this.note('发送「' + job.name + '」', '中断：' + msg + '（可重试续传）', -1);
    } finally {
      if (file !== null) {
        try {
          fs.closeSync(file);
        } catch (e) {
          // 忽略
        }
      }
    }
  }

  // 上传目标电脑 base
  private baseUrl: string = '';

  setBaseUrl(b: string): void {
    this.baseUrl = b;
  }
}

export function fmtSize(n: number): string {
  if (n >= 1073741824) {
    return (n / 1073741824).toFixed(1) + ' GB';
  }
  if (n >= 1048576) {
    return (n / 1048576).toFixed(1) + ' MB';
  }
  if (n >= 1024) {
    return (n / 1024).toFixed(1) + ' KB';
  }
  return n + ' B';
}
