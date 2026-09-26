// 키워드 · 알림 받은 글 · 설정을 data/slr-alarm.json 한 파일에 저장한다.
// 대시보드를 껐다 켜도 그대로 남는다.
import fs from 'node:fs';
import path from 'node:path';
import { norm } from './parse.js';

const MAX_HITS = 300;
const MAX_NOTIFIED = 1000;

export const DEFAULT_SETTINGS = {
  interval: 60, // 확인 간격(초)
  boardUrl: '', // 비우면 회원장터 > 팝니다
  paused: false,
  sound: true,
  telegramToken: '',
  telegramChatId: '',
  slrId: '', // 목록이 로그인해야 보일 때만
  slrPw: '',
};

export class Store {
  constructor(file) {
    this.file = file;
    this.data = this.read();
  }

  read() {
    let data = {};
    try {
      data = JSON.parse(fs.readFileSync(this.file, 'utf8'));
    } catch {
      // 처음 실행이거나 파일이 깨졌으면 빈 상태로 시작
    }
    return {
      keywords: Array.isArray(data.keywords) ? data.keywords : [],
      lastNo: Number(data.lastNo) || 0,
      notified: Array.isArray(data.notified) ? data.notified : [],
      hits: Array.isArray(data.hits) ? data.hits : [],
      settings: { ...DEFAULT_SETTINGS, ...(data.settings || {}) },
    };
  }

  save() {
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const tmp = `${this.file}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(this.data, null, 2));
    fs.renameSync(tmp, this.file);
  }

  get settings() {
    return this.data.settings;
  }

  updateSettings(patch) {
    for (const [key, value] of Object.entries(patch)) {
      if (key in DEFAULT_SETTINGS && value !== undefined) this.data.settings[key] = value;
    }
    this.save();
  }

  // ── 키워드 ──
  activeKeywords() {
    return this.data.keywords.filter((k) => k.on).map((k) => k.text);
  }

  findKeyword(text) {
    return this.data.keywords.find((k) => norm(k.text) === norm(text));
  }

  addKeywords(list) {
    const added = [];
    for (const text of list) {
      if (!text || this.findKeyword(text)) continue;
      this.data.keywords.push({ text, on: true, addedAt: new Date().toISOString() });
      added.push(text);
    }
    this.save();
    return added;
  }

  toggleKeyword(text) {
    const kw = this.findKeyword(text);
    if (!kw) return null;
    kw.on = !kw.on;
    this.save();
    return kw;
  }

  removeKeyword(text) {
    const before = this.data.keywords.length;
    this.data.keywords = this.data.keywords.filter((k) => norm(k.text) !== norm(text));
    this.save();
    return this.data.keywords.length < before;
  }

  // ── 알림 받은 글 ──
  addHit(post, keywords) {
    this.data.hits.unshift({ ...post, keywords, foundAt: new Date().toISOString(), read: false });
    this.data.hits = this.data.hits.slice(0, MAX_HITS);
    this.data.notified.push(post.no);
    this.data.notified = this.data.notified.slice(-MAX_NOTIFIED);
  }

  markRead(no) {
    for (const hit of this.data.hits) if (no === undefined || hit.no === no) hit.read = true;
    this.save();
  }

  clearHits() {
    this.data.hits = [];
    this.save();
  }
}
