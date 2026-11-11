import { promptAction } from '@kit.ArkUI';
import { common } from '@kit.AbilityKit';
import { scanBarcode } from '@kit.ScanKit';
import { picker, fileIo as fs } from '@kit.CoreFileKit';
import { pasteboard } from '@kit.BasicServicesKit';
import { Prefs, PcHistory } from '../model/Prefs';
import { UploadManager, UploadRowData, fmtSize } from '../model/UploadManager';
import { PcBeacon, startDiscovery, stopDiscovery } from '../model/Discovery';
import {
  MsgItem, FileItem, getVersion, getInfo, sendText, phoneMessages,
  sharedFiles, pcFiles, downloadToFile
} from '../model/Api';

const BLUE: string = '#007AFF';
const BG: string = '#F2F2F7';
const TEXT: string = '#1C1C1E';
const TEXT2: string = '#8E8E93';
const SEP: string = '#E5E5EA';
const FILL: string = '#E9E9EB';
/** 手动连接的默认电脑地址（本机当前局域网 IP + 服务端口） */
const DEFAULT_PC_ADDR: string = '192.168.1.37:8000';

function errMsg(e: Object): string {
  if (e instanceof Error) {
    return e.message;
  }
  return String(e);
}

function fmtTime(ms: number): string {
  const d = new Date(ms);
  const hh = d.getHours();
  const mm = d.getMinutes();
  const h: string = hh < 10 ? '0' + hh : '' + hh;
  const m: string = mm < 10 ? '0' + mm : '' + mm;
  return h + ':' + m;
}

function nameFromUri(uri: string): string {
  const idx = uri.lastIndexOf('/');
  let n: string = idx >= 0 ? uri.substring(idx + 1) : 'file';
  try {
    n = decodeURIComponent(n);
  } catch (e) {
    // 保留原样
  }
  return n === '' ? 'file' : n;
}

/** 二维码内容 http://ip:port/connect?name=xxx → base 地址 */
function baseFromQr(v: string): string {
  let u: string = v.trim();
  const i = u.indexOf('/connect');
  if (i > 0) {
    u = u.substring(0, i);
  }
  if (u.indexOf('http') === 0) {
    return u;
  }
  return 'http://' + u;
}

/**
 * 易码互传 · 鸿蒙客户端（ArkTS/ArkUI，iOS 风格卡片）
 * 未连接 → 连接页（扫码 / 手输 / 历史）；已连接 → 三 Tab（首页 / 消息 / 文件）
 */
@Entry
@Component
struct Index {
  @State base: string = '';
  @State titleStatus: string = '已连接';
  @State addrInput: string = '';
  @State textInput: string = '';
  @State verLine: string = '…';
  @State history: PcHistory[] = [];
  @State tab: number = 0;
  @State unread: number = 0;
  @State msgs: MsgItem[] = [];
  @State sharedList: FileItem[] = [];
  @State pcFilesList: FileItem[] = [];
  @State uploads: UploadRowData[] = [];
  @State dlShow: boolean = false;
  @State dlName: string = '';
  @State dlRecv: number = 0;
  @State dlTotal: number = 0;
  @State connecting: boolean = false;
  @State searching: boolean = false;
  @State foundName: string = '';
  private ctx: common.UIAbilityContext | null = null;
  private firstMsgLoad: boolean = true;
  private timer: number = -1;

  aboutToAppear(): void {
    const ctx: common.UIAbilityContext = getContext(this) as common.UIAbilityContext;
    this.ctx = ctx;
    UploadManager.instance().onUpdate = (rows: UploadRowData[]) => {
      this.uploads = rows;
    };
    this.uploads = UploadManager.instance().currentRows();
    Prefs.init(ctx).then(() => {
      this.history = Prefs.history();
      const b = Prefs.getBase();
      if (b !== '') {
        this.base = b;
        UploadManager.instance().setBaseUrl(b);
        this.firstMsgLoad = true;
        this.afterConnect();
      } else {
        // 未连接时预填手动地址：优先上次连接，其次默认网关地址
        const last: string = this.history.length > 0 ? this.history[0].base : DEFAULT_PC_ADDR;
        this.addrInput = last.indexOf('http') === 0 ? last.substring(last.indexOf('://') + 3) : last;
        // 并行启动局域网自动发现
        this.autoDiscover();
      }
    });
    this.timer = setInterval(() => {
      this.pollMsgs();
    }, 3000);
  }

  aboutToDisappear(): void {
    if (this.timer >= 0) {
      clearInterval(this.timer);
      this.timer = -1;
    }
    this.stopSearch();
  }

  onPageShow(): void {
    if (this.base !== '') {
      this.refreshFiles();
    }
  }

  toast(msg: string): void {
    promptAction.showToast({ message: msg, duration: 2000 });
  }

  // ================= 连接 =================

  /** 局域网自动发现：监听电脑的 UDP 58887 广播，收到即连 */
  async autoDiscover(): Promise<void> {
    if (this.searching || this.base !== '') {
      return;
    }
    this.searching = true;
    this.foundName = '';
    const ok: boolean = await startDiscovery((b: PcBeacon) => {
      if (this.base !== '' || this.connecting) {
        return;   // 已连上 / 正在连，忽略后续广播
      }
      this.foundName = b.name;
      this.stopSearch();
      this.toast('发现电脑「' + b.name + '」，正在连接…');
      this.tryConnect(b.base);
    });
    if (!ok) {
      // 端口被占等异常：静默回退到手动输入
      this.searching = false;
      return;
    }
    // 最长听 10 秒（电脑每 3 秒广播一次，两轮足够）
    setTimeout(() => {
      this.stopSearch();
    }, 10000);
  }

  stopSearch(): void {
    this.searching = false;
    stopDiscovery();
  }

  async tryConnect(b: string): Promise<void> {
    if (this.connecting) {
      return;
    }
    this.connecting = true;
    this.toast('正在连接…');
    try {
      const ver = await getVersion(b);
      this.base = b;
      Prefs.setBase(b);
      Prefs.addHistory(b, ver.name !== undefined ? ver.name : '电脑');
      this.history = Prefs.history();
      UploadManager.instance().setBaseUrl(b);
      this.firstMsgLoad = true;
      this.msgs = [];
      this.afterConnect();
      this.stopSearch();
      this.toast('已连接');
    } catch (e) {
      this.toast('连接失败：' + errMsg(e));
    }
    this.connecting = false;
  }

  async afterConnect(): Promise<void> {
    this.titleStatus = '已连接 · ' + this.base;
    try {
      const info = await getInfo(this.base);
      if (info.url !== undefined) {
        this.titleStatus = '已连接 · ' + info.url;
      }
    } catch (e) {
      // 保留 base 显示
    }
    try {
      const v = await getVersion(this.base);
      this.verLine = v.version_line !== undefined ? v.version_line : '-';
    } catch (e) {
      this.verLine = '-';
    }
    this.refreshFiles();
  }

  disconnect(): void {
    Prefs.clearBase();
    this.base = '';
    UploadManager.instance().setBaseUrl('');
    // 回到连接页后重新搜一次局域网
    this.autoDiscover();
  }

  async doScan(): Promise<void> {
    try {
      const options: scanBarcode.ScanOptions = {
        enableMultiMode: false,
        enableAlbum: true
      };
      const p: Promise<scanBarcode.ScanResult> = scanBarcode.startScanForResult(this.ctx, options);
      const result: scanBarcode.ScanResult = await p;
      const v: string = result.originalValue;
      if (v !== '') {
        this.tryConnect(baseFromQr(v));
      }
    } catch (e) {
      // 用户取消扫码，忽略
    }
  }

  removePc(b: string): void {
    AlertDialog.show({
      title: '移除电脑',
      message: '要从最近连接中移除 ' + b + ' 吗？',
      primaryButton: {
        value: '取消',
        action: () => {
        }
      },
      secondaryButton: {
        value: '移除',
        fontColor: BLUE,
        action: () => {
          Prefs.removeHistory(b);
          this.history = Prefs.history();
        }
      }
    });
  }

  // ================= 首页 =================

  async sendText(): Promise<void> {
    const txt: string = this.textInput;
    if (txt.length === 0) {
      this.toast('请先输入内容');
      return;
    }
    if (txt.length > 10000) {
      this.toast('内容过长（上限 1 万字）');
      return;
    }
    try {
      const r = await sendText(this.base, txt, Prefs.deviceName());
      if (r.ok === true) {
        this.textInput = '';
        this.toast('已发送，电脑已收到并复制');
      } else {
        this.toast('发送失败：' + (r.error !== undefined ? r.error : '未知错误'));
      }
    } catch (e) {
      this.toast('发送失败：网络错误');
    }
  }

  async pickFiles(): Promise<void> {
    if (this.base === '') {
      this.toast('请先连接电脑');
      return;
    }
    try {
      const doc = new picker.DocumentViewPicker();
      const opts: picker.DocumentSelectOptions = {
        maxSelectNumber: 9,
        fileSuffixFilters: []
      };
      const uris: string[] = await doc.select(opts);
      if (uris.length === 0) {
        return;
      }
      const mgr = UploadManager.instance();
      for (const uri of uris) {
        const nm: string = nameFromUri(uri);
        let size: number = 0;
        try {
          const f = fs.openSync(uri, fs.OpenMode.READ_ONLY);
          size = fs.statSync(f.fd).size;
          fs.closeSync(f);
        } catch (e) {
          // 大小未知时上传过程中会再取
        }
        mgr.enqueue(uri, nm, size);
      }
      this.tab = 0;
    } catch (e) {
      this.toast('未选择文件');
    }
  }

  pctOf(row: UploadRowData): number {
    if (row.size <= 0) {
      return 0;
    }
    return Math.floor(row.sent * 100 / row.size);
  }

  pctText(row: UploadRowData): string {
    if (row.state === 'queued') {
      return '排队中';
    }
    if (row.state === 'sending') {
      return this.pctOf(row) + '%';
    }
    if (row.state === 'done' || row.state === 'instant') {
      return '已完成';
    }
    if (row.state === 'canceled') {
      return '已取消';
    }
    return '已中断';
  }

  tipText(row: UploadRowData): string {
    if (row.state === 'queued') {
      return fmtSize(row.size);
    }
    if (row.state === 'sending') {
      return fmtSize(row.sent) + ' / ' + fmtSize(row.size);
    }
    if (row.state === 'done') {
      return fmtSize(row.size);
    }
    if (row.state === 'instant') {
      return '电脑上已有同文件（秒传）';
    }
    if (row.state === 'canceled') {
      return '已传部分保留，重试可续传';
    }
    return '可点重试续传';
  }

  // ================= 消息 =================

  async pollMsgs(): Promise<void> {
    if (this.base === '') {
      return;
    }
    try {
      const after: number = this.firstMsgLoad ? 0 : Prefs.lastMsgId();
      const r = await phoneMessages(this.base, after, Prefs.deviceId(), Prefs.deviceName());
      this.firstMsgLoad = false;
      if (r.ok !== true || r.messages === undefined) {
        return;
      }
      const arr: MsgItem[] = r.messages;
      if (arr.length === 0) {
        return;
      }
      let lastId: number = Prefs.lastMsgId();
      const list: MsgItem[] = this.msgs.slice();
      for (let i = 0; i < arr.length; i++) {
        list.unshift(arr[i]);
        if (arr[i].id > lastId) {
          lastId = arr[i].id;
        }
      }
      this.msgs = list;
      Prefs.setLastMsgId(lastId);
      if (this.tab !== 1) {
        this.unread += arr.length;
      }
    } catch (e) {
      // 网络波动忽略
    }
  }

  copyText(text: string): void {
    try {
      const data = pasteboard.createData(pasteboard.MIMETYPE_TEXT_PLAIN, text);
      pasteboard.getSystemPasteboard().setData(data).then(() => {
        this.toast('已复制');
      });
    } catch (e) {
      this.toast('复制失败');
    }
  }

  // ================= 文件 =================

  async refreshFiles(): Promise<void> {
    if (this.base === '') {
      return;
    }
    try {
      this.sharedList = await sharedFiles(this.base);
    } catch (e) {
      // 忽略
    }
    try {
      this.pcFilesList = await pcFiles(this.base);
    } catch (e) {
      // 忽略
    }
  }

  async downloadTo(path: string, name: string): Promise<void> {
    try {
      const actx = this.ctx;
      if (actx === null) {
        return;
      }
      const sp = new picker.DocumentViewPicker(actx);
      const sopt: picker.DocumentSaveOptions = {
        newFileNames: [name]
      };
      const uris: string[] = await sp.save(sopt);
      if (uris.length === 0) {
        return;
      }
      this.dlShow = true;
      this.dlName = name;
      this.dlRecv = 0;
      this.dlTotal = 0;
      await downloadToFile(this.base + path, uris[0], (recvd: number, total: number) => {
        this.dlRecv = recvd;
        this.dlTotal = total;
      });
      this.dlShow = false;
      this.toast('已保存：' + name);
    } catch (e) {
      this.dlShow = false;
      this.toast('下载失败：' + errMsg(e));
    }
  }

  // ================= Tab =================

  gotoTab(i: number): void {
    this.tab = i;
    if (i === 1) {
      this.unread = 0;
      this.pollMsgs();
    }
    if (i === 2) {
      this.refreshFiles();
    }
  }

  // ================= UI =================

  build() {
    Stack({ alignContent: Alignment.Center }) {
      Column() {
        if (this.base === '') {
          this.ConnectScreen()
        } else {
          this.MainScreen()
        }
      }
      .width('100%')
      .height('100%')
      .backgroundColor(BG)

      if (this.dlShow) {
        this.DownloadDialog()
      }
    }
    .width('100%')
    .height('100%')
  }

  @Builder
  ConnectScreen() {
    Scroll() {
      Column() {
        Text('易码互传')
          .fontSize(30)
          .fontWeight(FontWeight.Bold)
          .fontColor(TEXT)
          .margin({ top: 90, bottom: 6 })
        Text('手机 ⇄ 电脑 · 局域网互传')
          .fontSize(15)
          .fontColor(TEXT2)
          .margin({ bottom: 48 })

        Button('扫一扫 · 连接电脑')
          .fontSize(16)
          .fontWeight(FontWeight.Medium)
          .fontColor('#FFFFFF')
          .backgroundColor(BLUE)
          .borderRadius(12)
          .width('100%')
          .height(50)
          .onClick(() => {
            this.doScan();
          })

        // 局域网自动发现状态
        if (this.searching) {
          Row() {
            LoadingProgress()
              .width(18)
              .height(18)
              .color(TEXT2)
            Text('正在搜索局域网电脑…')
              .fontSize(13)
              .fontColor(TEXT2)
              .margin({ left: 8 })
          }
          .width('100%')
          .justifyContent(FlexAlign.Center)
          .margin({ top: 18 })
        } else if (this.foundName !== '') {
          Text('发现电脑「' + this.foundName + '」，正在自动连接…')
            .fontSize(13)
            .fontColor(BLUE)
            .width('100%')
            .textAlign(TextAlign.Center)
            .margin({ top: 18 })
        } else {
          Text('搜索局域网电脑')
            .fontSize(13)
            .fontColor(TEXT2)
            .width('100%')
            .textAlign(TextAlign.Center)
            .margin({ top: 18 })
            .onClick(() => {
              this.autoDiscover();
            })
        }

        Text('或')
          .fontSize(13)
          .fontColor(TEXT2)
          .margin({ top: 18, bottom: 18 })

        Column() {
          Text('手动输入电脑地址')
            .fontSize(13)
            .fontColor(TEXT2)
            .alignSelf(ItemAlign.Start)
          TextInput({ placeholder: DEFAULT_PC_ADDR, text: this.addrInput })
            .onChange((v: string) => {
              this.addrInput = v;
            })
            .backgroundColor(FILL)
            .borderRadius(10)
            .height(44)
            .margin({ top: 8 })
            .width('100%')
          Button('连接')
            .fontSize(14)
            .fontColor('#FFFFFF')
            .backgroundColor(BLUE)
            .borderRadius(8)
            .padding({ left: 18, right: 18, top: 9, bottom: 9 })
            .margin({ top: 12 })
            .alignSelf(ItemAlign.Start)
            .onClick(() => {
              const a: string = this.addrInput.trim();
              if (a === '') {
                this.toast('请输入地址');
                return;
              }
              this.tryConnect(a.indexOf('http') === 0 ? a : 'http://' + a);
            })
        }
        .backgroundColor('#FFFFFF')
        .borderRadius(12)
        .padding(16)
        .width('100%')
        .alignItems(HorizontalAlign.Start)

        if (this.history.length > 0) {
          Text('最近连接')
            .fontSize(18)
            .fontWeight(FontWeight.Bold)
            .fontColor(TEXT)
            .margin({ top: 32, bottom: 12 })
            .alignSelf(ItemAlign.Start)
          Column() {
            ForEach(this.history, (h: PcHistory) => {
              Row() {
                Column() {
                  Text(h.name === '' ? h.base : h.name)
                    .fontSize(15)
                    .fontColor(TEXT)
                    .maxLines(1)
                  Text(h.base)
                    .fontSize(13)
                    .fontColor(TEXT2)
                    .maxLines(1)
                }
                .layoutWeight(1)
                .alignItems(HorizontalAlign.Start)

                Text('⇢')
                  .fontSize(16)
                  .fontColor(BLUE)
              }
              .width('100%')
              .padding(16)
              .onClick(() => {
                this.tryConnect(h.base);
              })
              .gesture(
                LongPressGesture()
                  .onAction(() => {
                    this.removePc(h.base);
                  })
              )
              Divider()
                .color(SEP)
                .margin({ left: 16, right: 16 })
            }, (h: PcHistory) => h.base + '_' + h.ts)
          }
          .backgroundColor('#FFFFFF')
          .borderRadius(12)
          .width('100%')
          .alignItems(HorizontalAlign.Start)
        }

        Text('版权所有：易码通科技')
          .fontSize(12)
          .fontColor(TEXT2)
          .margin({ top: 24 })
      }
      .padding({ left: 24, right: 24 })
      .width('100%')
    }
    .scrollBar(BarState.Off)
    .width('100%')
    .height('100%')
  }

  @Builder
  MainScreen() {
    Column() {
      Column() {
        Text('易码互传')
          .fontSize(28)
          .fontWeight(FontWeight.Bold)
          .fontColor(TEXT)
        Text(this.titleStatus)
          .fontSize(14)
          .fontColor(TEXT2)
          .margin({ top: 4 })
      }
      .width('100%')
      .alignItems(HorizontalAlign.Start)
      .padding({ left: 24, right: 24, top: 18 })

      Stack() {
        if (this.tab === 0) {
          this.HomePage()
        }
        if (this.tab === 1) {
          this.MsgsPage()
        }
        if (this.tab === 2) {
          this.FilesPage()
        }
      }
      .layoutWeight(1)
      .width('100%')

      this.TabBar()
    }
    .width('100%')
    .height('100%')
  }

  @Builder
  TabBar() {
    Row() {
      this.TabItem(0, '⌂', '首页')
      this.TabItem(1, '✉', '消息')
      this.TabItem(2, '📦', '文件')
    }
    .width('100%')
    .backgroundColor('#FFFFFF')
  }

  @Builder
  TabItem(i: number, icon: string, name: string) {
    Column() {
      Text(this.unread > 0 && i === 1 ? icon + ' ' + this.unread : icon)
        .fontSize(18)
        .fontColor(i === this.tab ? BLUE : TEXT2)
      Text(name)
        .fontSize(11)
        .fontWeight(FontWeight.Bold)
        .fontColor(i === this.tab ? BLUE : TEXT2)
        .margin({ top: 2 })
    }
    .layoutWeight(1)
    .padding({ top: 7, bottom: 7 })
    .onClick(() => {
      this.gotoTab(i);
    })
  }

  @Builder
  HomePage() {
    Scroll() {
      Column() {
        Column() {
          Text('电脑连接')
            .fontSize(20)
            .fontWeight(FontWeight.Bold)
            .fontColor(TEXT)
            .alignSelf(ItemAlign.Start)
          Row() {
            Text('地址')
              .fontSize(14)
              .fontColor(TEXT2)
            Text(this.base)
              .fontSize(14)
              .fontColor(TEXT)
              .layoutWeight(1)
              .textAlign(TextAlign.End)
              .maxLines(1)
          }
          .width('100%')
          .margin({ top: 10 })
          Row() {
            Text('版本')
              .fontSize(14)
              .fontColor(TEXT2)
            Text(this.verLine)
              .fontSize(14)
              .fontColor(TEXT)
              .layoutWeight(1)
              .textAlign(TextAlign.End)
              .maxLines(1)
          }
          .width('100%')
          .margin({ top: 6 })
          Button('断开连接')
            .fontSize(14)
            .fontColor(BLUE)
            .backgroundColor(FILL)
            .borderRadius(8)
            .padding({ left: 18, right: 18, top: 9, bottom: 9 })
            .margin({ top: 14 })
            .alignSelf(ItemAlign.Start)
            .onClick(() => {
              this.disconnect();
            })
        }
        .backgroundColor('#FFFFFF')
        .borderRadius(12)
        .padding(16)
        .width('100%')
        .alignItems(HorizontalAlign.Start)

        Column() {
          Text('发文字到电脑')
            .fontSize(20)
            .fontWeight(FontWeight.Bold)
            .fontColor(TEXT)
            .alignSelf(ItemAlign.Start)
          Text('发送后电脑自动复制到剪贴板')
            .fontSize(13)
            .fontColor(TEXT2)
            .margin({ top: 4 })
            .alignSelf(ItemAlign.Start)
          TextArea({ placeholder: '网址、验证码、临时笔记…', text: this.textInput })
            .onChange((v: string) => {
              this.textInput = v;
            })
            .backgroundColor(FILL)
            .borderRadius(10)
            .margin({ top: 12 })
            .height(90)
            .width('100%')
          Button('发送')
            .fontSize(14)
            .fontColor('#FFFFFF')
            .backgroundColor(BLUE)
            .borderRadius(8)
            .padding({ left: 18, right: 18, top: 9, bottom: 9 })
            .margin({ top: 12 })
            .alignSelf(ItemAlign.Start)
            .onClick(() => {
              this.sendText();
            })
        }
        .backgroundColor('#FFFFFF')
        .borderRadius(12)
        .padding(16)
        .width('100%')
        .alignItems(HorizontalAlign.Start)
        .margin({ top: 14 })

        Column() {
          Text('发文件到电脑')
            .fontSize(20)
            .fontWeight(FontWeight.Bold)
            .fontColor(TEXT)
            .alignSelf(ItemAlign.Start)
          Text('分片传输 · 断点续传 · 后台不中断')
            .fontSize(13)
            .fontColor(TEXT2)
            .margin({ top: 4 })
            .alignSelf(ItemAlign.Start)
          Button('选择文件发送')
            .fontSize(16)
            .fontColor('#FFFFFF')
            .backgroundColor(BLUE)
            .borderRadius(12)
            .width('100%')
            .height(50)
            .margin({ top: 12 })
            .onClick(() => {
              this.pickFiles();
            })
          ForEach(this.uploads, (row: UploadRowData) => {
            this.UploadRow(row)
          }, (row: UploadRowData) => row.uri + '_' + row.state + '_' + row.sent)
        }
        .backgroundColor('#FFFFFF')
        .borderRadius(12)
        .padding(16)
        .width('100%')
        .alignItems(HorizontalAlign.Start)
        .margin({ top: 14 })

        Text('版权所有：易码通科技')
          .fontSize(12)
          .fontColor(TEXT2)
          .margin({ top: 24, bottom: 16 })
      }
      .padding({ left: 20, right: 20, top: 8 })
      .width('100%')
    }
    .scrollBar(BarState.Off)
    .width('100%')
    .height('100%')
  }

  @Builder
  UploadRow(row: UploadRowData) {
    Column() {
      Row() {
        Text(row.name)
          .fontSize(14)
          .fontColor(TEXT)
          .maxLines(1)
          .layoutWeight(1)
        Text(this.pctText(row))
          .fontSize(13)
          .fontColor(BLUE)
          .fontWeight(FontWeight.Bold)
      }
      .width('100%')
      Progress({
        value: row.state === 'done' || row.state === 'instant' ? 100 : this.pctOf(row),
        total: 100,
        type: ProgressType.Linear
      })
        .color(BLUE)
        .margin({ top: 8 })
        .width('100%')
      Row() {
        Text(this.tipText(row))
          .fontSize(12)
          .fontColor(TEXT2)
          .maxLines(1)
          .layoutWeight(1)
        if (row.state === 'queued' || row.state === 'sending') {
          Text('取消')
            .fontSize(13)
            .fontColor(BLUE)
            .padding({ left: 12, top: 8, bottom: 8 })
            .onClick(() => {
              UploadManager.instance().cancel(row.uri);
            })
        }
        if (row.state === 'canceled' || row.state === 'failed') {
          Text('重试')
            .fontSize(13)
            .fontColor(BLUE)
            .padding({ left: 12, top: 8, bottom: 8 })
            .onClick(() => {
              UploadManager.instance().retry(row.uri);
            })
        }
      }
      .width('100%')
      .margin({ top: 2 })
    }
    .width('100%')
    .padding({ top: 14 })
    .alignItems(HorizontalAlign.Start)
  }

  @Builder
  MsgsPage() {
    Scroll() {
      Column() {
        Row() {
          Text('电脑推送的文字')
            .fontSize(22)
            .fontWeight(FontWeight.Bold)
            .fontColor(TEXT)
            .layoutWeight(1)
          Button('清空')
            .fontSize(14)
            .fontColor(BLUE)
            .backgroundColor(FILL)
            .borderRadius(8)
            .padding({ left: 14, right: 14, top: 7, bottom: 7 })
            .onClick(() => {
              this.msgs = [];
            })
        }
        .width('100%')
        .padding({ top: 8, bottom: 14 })

        if (this.msgs.length === 0) {
          Text('暂无消息')
            .fontSize(14)
            .fontColor(TEXT2)
            .padding(48)
        } else {
          ForEach(this.msgs, (m: MsgItem) => {
            this.MsgCard(m)
          }, (m: MsgItem) => '' + m.id)
        }

        Text('每 3 秒自动刷新 · 仅前台轮询')
          .fontSize(12)
          .fontColor(TEXT2)
          .margin({ top: 20, bottom: 16 })
      }
      .padding({ left: 20, right: 20, top: 8 })
      .width('100%')
    }
    .scrollBar(BarState.Off)
    .width('100%')
    .height('100%')
  }

  @Builder
  MsgCard(m: MsgItem) {
    Column() {
      Text(m.text)
        .fontSize(15)
        .fontColor(TEXT)
        .width('100%')
        .alignSelf(ItemAlign.Start)
      Row() {
        Text(fmtTime(m.ts * 1000))
          .fontSize(12)
          .fontColor(TEXT2)
          .layoutWeight(1)
        Button('复制')
          .fontSize(14)
          .fontColor(BLUE)
          .backgroundColor(FILL)
          .borderRadius(8)
          .padding({ left: 14, right: 14, top: 5, bottom: 5 })
          .onClick(() => {
            this.copyText(m.text);
          })
      }
      .width('100%')
      .margin({ top: 10 })
    }
    .backgroundColor('#FFFFFF')
    .borderRadius(12)
    .padding(16)
    .width('100%')
    .margin({ bottom: 12 })
    .alignItems(HorizontalAlign.Start)
  }

  @Builder
  FilesPage() {
    Scroll() {
      Column() {
        Row() {
          Text('文件')
            .fontSize(22)
            .fontWeight(FontWeight.Bold)
            .fontColor(TEXT)
            .layoutWeight(1)
          Button('刷新')
            .fontSize(14)
            .fontColor(BLUE)
            .backgroundColor(FILL)
            .borderRadius(8)
            .padding({ left: 14, right: 14, top: 7, bottom: 7 })
            .onClick(() => {
              this.toast('正在刷新…');
              this.refreshFiles();
            })
        }
        .width('100%')
        .padding({ top: 8, bottom: 14 })

        Text('电脑分享给我的')
          .fontSize(16)
          .fontWeight(FontWeight.Bold)
          .fontColor(TEXT)
          .alignSelf(ItemAlign.Start)
        Column() {
          if (this.sharedList.length === 0) {
            Text('电脑尚未分享文件')
              .fontSize(14)
              .fontColor(TEXT2)
              .padding(24)
          } else {
            ForEach(this.sharedList, (f: FileItem) => {
              Column() {
                this.FileRow(f, true)
                Divider()
                  .color(SEP)
                  .margin({ left: 16, right: 16 })
              }
              .width('100%')
            }, (f: FileItem) => 's_' + f.id + '_' + f.name)
          }
        }
        .backgroundColor('#FFFFFF')
        .borderRadius(12)
        .width('100%')
        .margin({ top: 10 })
        .alignItems(HorizontalAlign.Start)

        Text('电脑接收目录')
          .fontSize(16)
          .fontWeight(FontWeight.Bold)
          .fontColor(TEXT)
          .alignSelf(ItemAlign.Start)
          .margin({ top: 24 })
        Column() {
          if (this.pcFilesList.length === 0) {
            Text('暂无文件')
              .fontSize(14)
              .fontColor(TEXT2)
              .padding(24)
          } else {
            ForEach(this.pcFilesList, (f: FileItem) => {
              Column() {
                this.FileRow(f, false)
                Divider()
                  .color(SEP)
                  .margin({ left: 16, right: 16 })
              }
              .width('100%')
            }, (f: FileItem) => 'f_' + f.name + '_' + f.size)
          }
        }
        .backgroundColor('#FFFFFF')
        .borderRadius(12)
        .width('100%')
        .margin({ top: 10 })
        .alignItems(HorizontalAlign.Start)

        Text('点击文件即可保存到手机')
          .fontSize(12)
          .fontColor(TEXT2)
          .margin({ top: 20, bottom: 16 })
      }
      .padding({ left: 20, right: 20, top: 8 })
      .width('100%')
    }
    .scrollBar(BarState.Off)
    .width('100%')
    .height('100%')
  }

  @Builder
  FileRow(f: FileItem, shared: boolean) {
    Row() {
      Column() {
        Text(f.name)
          .fontSize(15)
          .fontColor(TEXT)
          .maxLines(1)
        Text(fmtSize(f.size))
          .fontSize(13)
          .fontColor(TEXT2)
      }
      .layoutWeight(1)
      .alignItems(HorizontalAlign.Start)

      Text('⇩')
        .fontSize(18)
        .fontColor(BLUE)
    }
    .width('100%')
    .padding(16)
    .onClick(() => {
      const path: string = shared
        ? ('/share/' + encodeURIComponent(f.id !== undefined ? f.id : ''))
        : ('/download/' + encodeURIComponent(f.name));
      this.downloadTo(path, f.name);
    })
  }

  @Builder
  DownloadDialog() {
    Column() {
      Text('下载到手机')
        .fontSize(16)
        .fontWeight(FontWeight.Bold)
        .fontColor(TEXT)
        .margin({ bottom: 12 })
      Text(this.dlName)
        .fontSize(13)
        .fontColor(TEXT2)
        .maxLines(1)
        .margin({ bottom: 8 })
      Progress({
        value: this.dlTotal > 0 ? Math.floor(this.dlRecv * 100 / this.dlTotal) : 0,
        total: 100,
        type: ProgressType.Linear
      })
        .color(BLUE)
        .width('100%')
      Text(this.dlTotal > 0
        ? (Math.floor(this.dlRecv * 100 / this.dlTotal) + '% · ' + fmtSize(this.dlRecv) + ' / ' + fmtSize(this.dlTotal))
        : fmtSize(this.dlRecv))
        .fontSize(13)
        .fontColor(TEXT2)
        .margin({ top: 8 })
    }
    .width('80%')
    .backgroundColor('#FFFFFF')
    .borderRadius(12)
    .padding(20)
    .alignItems(HorizontalAlign.Center)
  }
}
