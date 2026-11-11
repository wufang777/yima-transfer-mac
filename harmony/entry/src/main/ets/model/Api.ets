import { http } from '@kit.NetworkKit';
import { fileIo as fs } from '@kit.CoreFileKit';

/**
 * 手机端 HTTP 协议封装（对应电脑端 app.py / yima_win.py）。
 * base 形如 http://192.168.1.5:8000
 */
export const CHUNK_SIZE: number = 2 * 1024 * 1024; // 2MB 分片，与服务端协议一致

export interface VersionResp {
  ok?: boolean;
  version?: string;
  build?: string;
  version_line?: string;
  name?: string;
}

export interface InfoResp {
  url?: string;
  dir?: string;
}

export interface TextResp {
  ok?: boolean;
  error?: string;
}

export interface UploadInitResp {
  ok?: boolean;
  id?: string;
  offset?: number;
  done?: boolean;
  error?: string;
}

interface ChunkResp {
  ok?: boolean;
  offset?: number;
  error?: string;
}

export interface MsgItem {
  id: number;
  text: string;
  ts: number;
}

export interface MsgResp {
  ok?: boolean;
  messages?: MsgItem[];
}

export interface FileItem {
  name: string;
  size: number;
  id?: string;
  folder?: string;
}

// ---------------- 基础工具 ----------------

function errMsg(e: Object): string {
  if (e instanceof Error) {
    return e.message;
  }
  return String(e);
}

function respText(resp: http.HttpResponse): string {
  if (typeof resp.result === 'string') {
    return resp.result;
  }
  return JSON.stringify(resp.result);
}

async function rawRequest(url: string, opt: http.HttpRequestOptions): Promise<http.HttpResponse> {
  const h = http.createHttp();
  try {
    return await h.request(url, opt);
  } finally {
    h.destroy();
  }
}

async function reqJson(url: string, opt: http.HttpRequestOptions): Promise<object> {
  const resp = await rawRequest(url, opt);
  const code = resp.responseCode;
  const text = respText(resp);
  let obj: object;
  try {
    obj = JSON.parse(text) as object;
  } catch (e) {
    throw new Error('HTTP ' + code);
  }
  if (code < 200 || code >= 300) {
    const er = obj as TextResp;
    throw new Error(er.error ? er.error : ('HTTP ' + code));
  }
  return obj;
}

function getJson(url: string): Promise<object> {
  const opt: http.HttpRequestOptions = {
    method: http.RequestMethod.GET,
    connectTimeout: 6000,
    readTimeout: 20000,
    expectDataType: http.HttpDataType.STRING
  };
  return reqJson(url, opt);
}

function postJson(url: string, body: object): Promise<object> {
  const header: Record<string, string> = { 'Content-Type': 'application/json; charset=utf-8' };
  const opt: http.HttpRequestOptions = {
    method: http.RequestMethod.POST,
    header: header,
    extraData: JSON.stringify(body),
    connectTimeout: 8000,
    readTimeout: 30000,
    expectDataType: http.HttpDataType.STRING
  };
  return reqJson(url, opt);
}

// ---------------- 业务接口 ----------------

/** 探活 + 取版本；失败抛异常 */
export async function getVersion(base: string): Promise<VersionResp> {
  return (await getJson(base + '/api/version')) as VersionResp;
}

/** 连接信息 */
export async function getInfo(base: string): Promise<InfoResp> {
  return (await getJson(base + '/info')) as InfoResp;
}

/** 发文字到电脑（电脑自动复制）；from 用设备名以便电脑端区分多台手机 */
export async function sendText(base: string, text: string, from?: string): Promise<TextResp> {
  const body: Record<string, string> = { 'text': text, 'from': from !== undefined && from !== '' ? from : '手机' };
  return (await postJson(base + '/api/text/receive', body)) as TextResp;
}

/** 电脑推送的文字（after=上次最大 id；did+name 作为设备身份随轮询上报心跳） */
export async function phoneMessages(base: string, after: number, did?: string, name?: string): Promise<MsgResp> {
  let url = base + '/api/text/phone/messages?after=' + after;
  if (did !== undefined && did !== '') {
    url += '&did=' + encodeURIComponent(did);
    if (name !== undefined && name !== '') {
      url += '&name=' + encodeURIComponent(name);
    }
  }
  return (await getJson(url)) as MsgResp;
}

/** 电脑分享给手机的文件 */
export async function sharedFiles(base: string): Promise<FileItem[]> {
  const obj = await getJson(base + '/api/shared');
  return obj as FileItem[];
}

/** 电脑接收目录里的文件 */
export async function pcFiles(base: string): Promise<FileItem[]> {
  const obj = await getJson(base + '/files');
  return obj as FileItem[];
}

/** 上传初始化：同名同大小未完成 → 断点续传；done=true → 秒传 */
export async function uploadInit(base: string, name: string, size: number): Promise<UploadInitResp> {
  const body: Record<string, Object> = { 'name': name, 'size': size };
  return (await postJson(base + '/upload/init', body)) as UploadInitResp;
}

/**
 * 上传一个分片；409 时按服务端权威偏移对齐；成功返回新 offset。
 * 注意：必须用 Content-Length 定长（ArrayBuffer 作为 extraData 时网络库自动设置），
 * 服务端不认 chunked 编码（见安卓端同款踩坑记录）。
 */
export async function uploadChunk(
  base: string, id: string, offset: number, buf: ArrayBuffer, len: number
): Promise<number> {
  const url = base + '/upload/chunk?id=' + encodeURIComponent(id) + '&offset=' + offset;
  let body: ArrayBuffer = buf;
  if (len < buf.byteLength) {
    body = buf.slice(0, len);
  }
  const header: Record<string, string> = { 'Content-Type': 'application/octet-stream' };
  const opt: http.HttpRequestOptions = {
    method: http.RequestMethod.POST,
    header: header,
    extraData: body,
    connectTimeout: 15000,
    readTimeout: 60000,
    expectDataType: http.HttpDataType.STRING
  };
  const resp = await rawRequest(url, opt);
  const j = JSON.parse(respText(resp)) as ChunkResp;
  if (resp.responseCode === 409) {
    return j.offset !== undefined ? j.offset : offset;
  }
  if (resp.responseCode < 200 || resp.responseCode >= 300) {
    throw new Error('HTTP ' + resp.responseCode);
  }
  if (j.ok === false) {
    throw new Error(j.error ? j.error : '分片上传失败');
  }
  return j.offset !== undefined ? j.offset : offset;
}

/** 上传完成校验 */
export async function uploadComplete(base: string, id: string): Promise<TextResp> {
  const body: Record<string, string> = { 'id': id };
  return (await postJson(base + '/upload/complete', body)) as TextResp;
}

/**
 * 流式下载到本地文件（DocumentSavePicker 返回的 uri），带进度回调。
 */
export async function downloadToFile(
  url: string, saveUri: string, onProgress: (recvd: number, total: number) => void
): Promise<void> {
  return new Promise<void>((resolve: () => void, reject: (reason: Error) => void) => {
    const h = http.createHttp();
    let total: number = -1;
    let recvd: number = 0;
    let dst: fs.File;
    // 先打开目标文件，再发请求，避免数据先到写不进去
    try {
      dst = fs.openSync(saveUri, fs.OpenMode.READ_WRITE | fs.OpenMode.TRUNC);
    } catch (e) {
      h.destroy();
      reject(new Error('无法写入目标文件：' + errMsg(e)));
      return;
    }
    const cleanup = (): void => {
      try {
        fs.closeSync(dst);
      } catch (e) {
        // 忽略关闭异常
      }
      try {
        h.destroy();
      } catch (e) {
        // 忽略
      }
    };
    h.on('headersReceive', (header: Object) => {
      try {
        const m = header as Record<string, Object>;
        const cl = m['content-length'];
        if (cl !== undefined && cl !== null) {
          total = parseInt(String(cl));
        }
      } catch (e) {
        // 忽略
      }
    });
    h.on('dataReceive', (chunk: ArrayBuffer) => {
      fs.writeSync(dst.fd, chunk);
      recvd += chunk.byteLength;
      onProgress(recvd, total);
    });
    h.on('dataEnd', () => {
      // 收尾由 requestInStream 的返回结果处理
    });
    h.requestInStream(url, {
      method: http.RequestMethod.GET,
      connectTimeout: 15000,
      readTimeout: 60000
    }).then((code: number) => {
      if (code < 200 || code >= 300) {
        cleanup();
        reject(new Error('HTTP ' + code));
      } else {
        cleanup();
        resolve();
      }
    }).catch((e: Object) => {
      cleanup();
      reject(new Error(errMsg(e)));
    });
  });
}
