import { expect, test } from "bun:test";
import { handle, presign } from "../../supabase/functions/reader-voice/index.ts";

const ENV = {
  SUPABASE_URL: "https://sb.test", SUPABASE_SERVICE_ROLE_KEY: "svc-test",
  R2_ENDPOINT: "https://acc.r2.cloudflarestorage.com", R2_BUCKET: "reader-books", R2_KEY_ID: "kid", R2_SECRET: "sec",
  R2_PUBLIC_BASE: "https://pub-x.r2.dev", GITHUB_PAT: "ghp-test",
};

type Call = { url: string; method: string; headers: Record<string, string>; body?: any };
function fake(opts: { code?: string; busy?: boolean; locked?: boolean; ghStatus?: number; epubLen?: number | null; link?: string | null; delStatus?: number; cancelOk?: boolean; runs?: number[]; badAudience?: boolean } = {}) {
  const calls: Call[] = [];
  const f = (async (url: any, init: any = {}) => {
    const u = String(url);
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls.push({ url: u, method: init.method || "GET", headers: init.headers || {}, body });
    const resp = (status: number, data: any, h: Record<string, string> = {}) => new Response(data === undefined ? null : JSON.stringify(data), { status, headers: h });
    if (u.endsWith("/rpc/reader_voice_auth")) {
      if (opts.locked) return resp(400, { code: "54000", message: "too many attempts" });
      return resp(200, body.p_code === (opts.code ?? "good-code"));
    }
    if (u.endsWith("/rpc/reader_job_create")) return opts.badAudience ? resp(400, { code: "22023", message: "bad audience" }) : opts.busy ? resp(400, { code: "55006", message: "busy" }) : resp(200, 42);
    if (u.endsWith("/rpc/reader_job_fail")) return resp(200, null);
    if (u.endsWith("/rpc/reader_voice_list")) return resp(200, { jobs: [], books: [{ id: 1, title: "Чорти", link: "https://x" }] });
    if (u.endsWith("/rpc/reader_book_link")) return resp(200, opts.link === undefined ? "https://pub-x.r2.dev/tok/Book%20One.audiobook.zip" : opts.link);
    if (u.endsWith("/rpc/reader_book_delete")) return resp(200, true);
    if (u.endsWith("/rpc/reader_job_cancel")) return resp(200, opts.cancelOk ?? true);
    if (u.endsWith("/rpc/reader_progress_set")) return resp(200, null);
    if (u.endsWith("/rpc/reader_progress_get")) return resp(200, body.p_book === "known000001" ? { pos: 900, total: 40000, updated: "2026-10-28T10:00:00Z" } : null);
    if (u.includes("/actions/workflows/voice.yml/runs?")) return resp(200, { workflow_runs: u.includes("status=in_progress") ? (opts.runs ?? [555]).map(id => ({ id })) : [] });
    if (u.includes("/actions/runs/") && u.endsWith("/cancel")) return new Response(null, { status: 202 });
    if (u.startsWith("https://acc.r2.cloudflarestorage.com/")) return new Response(null, { status: opts.delStatus ?? 204 });
    if (u.startsWith("https://pub-x.r2.dev/")) return opts.epubLen === null ? resp(404, undefined) : resp(200, undefined, { "content-length": String(opts.epubLen ?? 5000000) });
    if (u.startsWith("https://api.github.com/")) return new Response(null, { status: opts.ghStatus ?? 204 });
    throw new Error("несподіваний запит " + u);
  }) as unknown as typeof fetch;
  return { f, calls };
}
const post = (b: any) => new Request("https://fn.test/reader-voice", { method: "POST", body: JSON.stringify(b) });
const KEY = "in/" + "a".repeat(32) + "/book.epub";

test("підпис збігається з прикладом із документації AWS", async () => {
  const url = await presign({ method: "GET", host: "examplebucket.s3.amazonaws.com", path: "/test.txt", region: "us-east-1",
    keyId: "AKIAIOSFODNN7EXAMPLE", secret: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", expires: 86400, now: new Date("2013-05-24T00:00:00Z") });
  expect(url).toContain("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404");
});

test("неправильний код: 401 і жодних дій", async () => {
  const { f, calls } = fake();
  const r = await handle(post({ action: "presign", code: "bad", size: 5000000 }), ENV, f);
  expect(r.status).toBe(401);
  expect(calls.length).toBe(1);
});

test("блокування після багатьох спроб: 429", async () => {
  const { f } = fake({ locked: true });
  expect((await handle(post({ action: "check", code: "x" }), ENV, f)).status).toBe(429);
});

test("check і list з правильним кодом", async () => {
  const { f } = fake();
  expect((await (await handle(post({ action: "check", code: "good-code" }), ENV, f)).json()).ok).toBe(true);
  const l = await (await handle(post({ action: "list", code: "good-code" }), ENV, f)).json();
  expect(l.books[0].title).toBe("Чорти");
});

test("presign: ключ у in/, посилання на бакет, розмір обмежено", async () => {
  const { f } = fake();
  const r = await handle(post({ action: "presign", code: "good-code", size: 5000000 }), ENV, f, () => new Date("2026-10-23T12:00:00Z"));
  const j = await r.json();
  expect(j.key).toMatch(/^in\/[a-f0-9]{32}\/book\.epub$/);
  expect(j.put_url.startsWith("https://acc.r2.cloudflarestorage.com/reader-books/" + j.key + "?")).toBe(true);
  expect(j.put_url).toContain("X-Amz-Expires=900");
  expect(j.put_url).not.toContain("sec");   // секрет у посилання не потрапляє
  expect((await handle(post({ action: "presign", code: "good-code", size: 200 * 1024 * 1024 }), ENV, f)).status).toBe(400);
  expect((await handle(post({ action: "presign", code: "good-code", size: 10 }), ENV, f)).status).toBe(400);
});

test("run: створює задачу й запускає workflow з правильними параметрами", async () => {
  const { f, calls } = fake();
  const r = await handle(post({ action: "run", code: "good-code", key: KEY, title: "Чорти.epub", gender: "male" }), ENV, f);
  expect(r.status).toBe(200);
  expect((await r.json()).job).toBe(42);
  const create = calls.find(c => c.url.endsWith("reader_job_create"))!;
  expect(create.body).toEqual({ p_title: "Чорти", p_gender: "male", p_url: "https://pub-x.r2.dev/" + KEY, p_for: "both" });
  const gh = calls.find(c => c.url.includes("api.github.com"))!;
  expect(gh.url).toBe("https://api.github.com/repos/abohdanovskyi/reader-app/actions/workflows/voice.yml/dispatches");
  expect(gh.headers.Authorization).toBe("Bearer ghp-test");
  expect(gh.body).toEqual({ ref: "dev", inputs: { epub_url: "https://pub-x.r2.dev/" + KEY, gender: "male", jobs: "12", job_id: "42" } });
});

test("list: «хто питає» передається в базу, некоректне значення ігнорується", async () => {
  let x = fake();
  await handle(post({ action: "list", code: "good-code", me: "ksenia" }), ENV, x.f);
  expect(x.calls.find(c => c.url.endsWith("reader_voice_list"))!.body).toEqual({ p_me: "ksenia" });
  x = fake();
  await handle(post({ action: "list", code: "good-code", me: "x'; drop table" }), ENV, x.f);
  expect(x.calls.find(c => c.url.endsWith("reader_voice_list"))!.body).toEqual({ p_me: null });
  x = fake();
  await handle(post({ action: "list", code: "good-code" }), ENV, x.f);
  expect(x.calls.find(c => c.url.endsWith("reader_voice_list"))!.body).toEqual({ p_me: null });
});

test("run: «для кого» передається в базу; без вибору книга спільна; сміття відхиляється; невідомий ключ це 400", async () => {
  let x = fake();
  expect((await handle(post({ action: "run", code: "good-code", key: KEY, for: "ksenia" }), ENV, x.f)).status).toBe(200);
  expect(x.calls.find(c => c.url.endsWith("reader_job_create"))!.body.p_for).toBe("ksenia");
  x = fake();
  await handle(post({ action: "run", code: "good-code", key: KEY }), ENV, x.f);
  expect(x.calls.find(c => c.url.endsWith("reader_job_create"))!.body.p_for).toBe("both");
  x = fake();
  const bad = await handle(post({ action: "run", code: "good-code", key: KEY, for: "A B;" }), ENV, x.f);
  expect(bad.status).toBe(400);
  expect(x.calls.some(c => c.url.endsWith("reader_job_create"))).toBe(false);
  x = fake({ badAudience: true });
  const unknown = await handle(post({ action: "run", code: "good-code", key: KEY, for: "nobody" }), ENV, x.f);
  expect(unknown.status).toBe(400);
  expect(x.calls.some(c => c.url.includes("api.github.com"))).toBe(false);
});

test("run: голос Apple передається далі, невідомий голос стає жіночим", async () => {
  let x = fake();
  await handle(post({ action: "run", code: "good-code", key: KEY, gender: "apple" }), ENV, x.f);
  expect(x.calls.find(c => c.url.endsWith("reader_job_create"))!.body.p_gender).toBe("apple");
  expect(x.calls.find(c => c.url.includes("api.github.com"))!.body.inputs.gender).toBe("apple");
  x = fake();
  await handle(post({ action: "run", code: "good-code", key: KEY, gender: "robot" }), ENV, x.f);
  expect(x.calls.find(c => c.url.includes("api.github.com"))!.body.inputs.gender).toBe("female");
});

test("run: чужий ключ файлу, відсутній файл, зайнято, відмова GitHub", async () => {
  let { f } = fake();
  expect((await handle(post({ action: "run", code: "good-code", key: "../x" }), ENV, f)).status).toBe(400);
  expect((await handle(post({ action: "run", code: "good-code", key: "in/" + "a".repeat(32) + "/book.epub/../../x" }), ENV, f)).status).toBe(400);
  ({ f } = fake({ epubLen: null }));
  expect((await handle(post({ action: "run", code: "good-code", key: KEY }), ENV, f)).status).toBe(400);
  ({ f } = fake({ busy: true }));
  expect((await handle(post({ action: "run", code: "good-code", key: KEY }), ENV, f)).status).toBe(409);
  const x = fake({ ghStatus: 401 });
  const r = await handle(post({ action: "run", code: "good-code", key: KEY }), ENV, x.f);
  expect(r.status).toBe(502);
  const fail = x.calls.find(c => c.url.endsWith("reader_job_fail"))!;
  expect(fail.body.p_id).toBe(42);
  expect(fail.body.p_err).toContain("401");
});

test("секрети не потрапляють у відповіді; CORS для браузера", async () => {
  const { f } = fake();
  const o = await handle(new Request("https://fn.test", { method: "OPTIONS" }), ENV, f);
  expect(o.status).toBe(204);
  expect(o.headers.get("access-control-allow-origin")).toBe("*");
  const r = await handle(post({ action: "run", code: "good-code", key: KEY }), ENV, f);
  const t = await r.text();
  for (const s of ["ghp-test", "svc-test", "sec"]) expect(t).not.toContain(s);
});

test("delete: видаляє файл у R2 (підписаний DELETE) і запис у базі", async () => {
  const { f, calls } = fake();
  const r = await handle(post({ action: "delete", code: "good-code", id: 7 }), ENV, f);
  expect(r.status).toBe(200);
  const del = calls.find(c => c.method === "DELETE")!;
  expect(del.url).toContain("/reader-books/tok/Book%20One.audiobook.zip?");
  expect(del.url).toContain("X-Amz-Signature=");
  expect(calls.map(c => c.url.split("/").pop()!.split("?")[0])).toEqual(["reader_voice_auth", "reader_book_link", "Book%20One.audiobook.zip", "reader_book_delete"]);
  expect(calls[3].body.p_id).toBe(7);
});

test("delete: якщо файл не видалився, запис у базі лишається", async () => {
  const { f, calls } = fake({ delStatus: 403 });
  const r = await handle(post({ action: "delete", code: "good-code", id: 7 }), ENV, f);
  expect(r.status).toBe(502);
  expect(calls.some(c => c.url.endsWith("reader_book_delete"))).toBe(false);
});

test("delete: чужі посилання і вхідні EPUB у R2 не чіпаємо, запис прибираємо", async () => {
  for (const link of ["https://other.example/x.audiobook.zip", "https://pub-x.r2.dev/in/" + "a".repeat(32) + "/book.epub", "https://pub-x.r2.dev/a/../b.audiobook.zip"]) {
    const { f, calls } = fake({ link });
    const r = await handle(post({ action: "delete", code: "good-code", id: 3 }), ENV, f);
    expect(r.status).toBe(200);
    expect(calls.some(c => c.method === "DELETE")).toBe(false);
    expect(calls.some(c => c.url.endsWith("reader_book_delete"))).toBe(true);
  }
});

test("delete: нема книги або некоректний id", async () => {
  expect((await handle(post({ action: "delete", code: "good-code", id: 9 }), ENV, fake({ link: null }).f)).status).toBe(404);
  expect((await handle(post({ action: "delete", code: "good-code", id: "x" }), ENV, fake().f)).status).toBe(400);
});

test("cancel: спершу позначає задачу в базі, потім зупиняє запуск у GitHub", async () => {
  const { f, calls } = fake();
  const r = await handle(post({ action: "cancel", code: "good-code", id: 42 }), ENV, f);
  expect(r.status).toBe(200);
  expect((await r.json()).stopped).toBe(1);
  const iDb = calls.findIndex(c => c.url.endsWith("reader_job_cancel"));
  const iGh = calls.findIndex(c => c.url.endsWith("/actions/runs/555/cancel"));
  expect(iDb).toBeGreaterThan(-1);
  expect(iGh).toBeGreaterThan(iDb);
  expect(calls[iDb].body).toEqual({ p_id: 42 });
  expect(calls[iGh].method).toBe("POST");
  expect(calls[iGh].headers.Authorization).toBe("Bearer ghp-test");
});

test("cancel: задача вже завершилась, неправильний id, неправильний код", async () => {
  let x = fake({ cancelOk: false });
  const r = await handle(post({ action: "cancel", code: "good-code", id: 42 }), ENV, x.f);
  expect(r.status).toBe(404);
  expect(x.calls.some(c => c.url.includes("/cancel"))).toBe(false);   // нічого в GitHub не чіпаємо
  expect((await handle(post({ action: "cancel", code: "good-code", id: "x" }), ENV, fake().f)).status).toBe(400);
  expect((await handle(post({ action: "cancel", code: "good-code", id: -3 }), ENV, fake().f)).status).toBe(400);
  x = fake();
  expect((await handle(post({ action: "cancel", code: "bad", id: 42 }), ENV, x.f)).status).toBe(401);
  expect(x.calls.some(c => c.url.includes("reader_job_cancel"))).toBe(false);
});

test("progress: зберігає й читає позицію за профілем, перевіряє дані", async () => {
  let x = fake();
  expect((await handle(post({ action: "progress_set", code: "good-code", profile: " Анна ", book: "known000001", pos: 123.4, total: 40000 }), ENV, x.f)).status).toBe(200);
  expect(x.calls.find(c => c.url.endsWith("reader_progress_set"))!.body).toEqual({ p_profile: "Анна", p_book: "known000001", p_pos: 123.4, p_total: 40000 });
  const g = await (await handle(post({ action: "progress_get", code: "good-code", profile: "Анна", book: "known000001" }), ENV, fake().f)).json();
  expect(g.progress.pos).toBe(900);
  expect((await (await handle(post({ action: "progress_get", code: "good-code", profile: "Анна", book: "other0000001" }), ENV, fake().f)).json()).progress).toBeNull();
  for (const bad of [{ profile: "", book: "known000001" }, { profile: "x".repeat(41), book: "known000001" }, { profile: "К", book: "../etc" }, { profile: "К", book: "ab" }]) {
    expect((await handle(post({ action: "progress_get", code: "good-code", ...bad }), ENV, fake().f)).status).toBe(400);
  }
  for (const bad of [{ pos: -1, total: 5 }, { pos: "abc", total: 5 }, { pos: 5, total: 1e9 }]) {
    expect((await handle(post({ action: "progress_set", code: "good-code", profile: "К", book: "known000001", ...bad }), ENV, fake().f)).status).toBe(400);
  }
  x = fake();
  expect((await handle(post({ action: "progress_set", code: "bad", profile: "К", book: "known000001", pos: 1, total: 2 }), ENV, x.f)).status).toBe(401);
});
