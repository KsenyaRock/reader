// Edge Function «reader-voice»: запуск хмарної озвучки з читалки. Вимкнути «Verify JWT» (доступ за кодом запуску).
// Дії (POST JSON {action, code, ...}): check, presign, run, list, delete, cancel, progress_get, progress_set.
// Секрети функції (Dashboard -> Edge Functions -> Secrets): R2_ENDPOINT, R2_BUCKET, R2_KEY_ID, R2_SECRET, R2_PUBLIC_BASE, GITHUB_PAT,
// необов'язково GITHUB_REPO (abohdanovskyi/reader-app) і GITHUB_REF (dev). SUPABASE_URL і SUPABASE_SERVICE_ROLE_KEY Supabase додає сам.
// Усі значення беруться лише зі змінних середовища, у коді секретів немає.

const MAX_EPUB = 150 * 1024 * 1024;
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "content-type, authorization, apikey, x-client-info",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

const enc = new TextEncoder();
const hex = (b: ArrayBuffer) => [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, "0")).join("");
const sha256hex = async (s: string) => hex(await crypto.subtle.digest("SHA-256", enc.encode(s)));
async function hmac(key: ArrayBuffer | Uint8Array, data: string): Promise<ArrayBuffer> {
  const k = await crypto.subtle.importKey("raw", key, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return crypto.subtle.sign("HMAC", k, enc.encode(data));
}
const rfc3986 = (s: string) => encodeURIComponent(s).replace(/[!'()*]/g, c => "%" + c.charCodeAt(0).toString(16).toUpperCase());

// Підписане посилання AWS SigV4 (query-підпис, лише заголовок host підписується; тіло UNSIGNED-PAYLOAD)
export async function presign(o: { method: string; host: string; path: string; region: string; keyId: string; secret: string; expires: number; now: Date }): Promise<string> {
  const iso = o.now.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
  const day = iso.slice(0, 8);
  const scope = `${day}/${o.region}/s3/aws4_request`;
  const q: [string, string][] = [
    ["X-Amz-Algorithm", "AWS4-HMAC-SHA256"],
    ["X-Amz-Credential", `${o.keyId}/${scope}`],
    ["X-Amz-Date", iso],
    ["X-Amz-Expires", String(o.expires)],
    ["X-Amz-SignedHeaders", "host"],
  ];
  q.sort((a, b) => (a[0] < b[0] ? -1 : 1));
  const canonicalQuery = q.map(([k, v]) => `${rfc3986(k)}=${rfc3986(v)}`).join("&");
  const canonicalPath = o.path.split("/").map(rfc3986).join("/");
  const canonical = [o.method, canonicalPath, canonicalQuery, `host:${o.host}\n`, "host", "UNSIGNED-PAYLOAD"].join("\n");
  const toSign = ["AWS4-HMAC-SHA256", iso, scope, await sha256hex(canonical)].join("\n");
  let k: ArrayBuffer = await hmac(enc.encode("AWS4" + o.secret), day);
  for (const part of [o.region, "s3", "aws4_request"]) k = await hmac(k, part);
  const sig = hex(await hmac(k, toSign));
  return `https://${o.host}${canonicalPath}?${canonicalQuery}&X-Amz-Signature=${sig}`;
}

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { ...CORS, "Content-Type": "application/json" } });

type Env = Record<string, string | undefined>;

export async function handle(req: Request, env: Env, f: typeof fetch = fetch, now: () => Date = () => new Date()): Promise<Response> {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
  if (req.method !== "POST") return json(405, { ok: false, error: "Лише POST" });
  let b: Record<string, unknown>;
  try { b = await req.json(); } catch { return json(400, { ok: false, error: "Некоректний запит" }); }
  const action = String(b.action || "");
  const sb = (env.SUPABASE_URL || "").replace(/\/$/, "");
  const svc = env.SUPABASE_SERVICE_ROLE_KEY || "";
  const rpc = async (name: string, args: unknown) => {
    const r = await f(`${sb}/rest/v1/rpc/${name}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", apikey: svc, Authorization: "Bearer " + svc },
      body: JSON.stringify(args),
    });
    const text = await r.text();
    let data: any = null;
    try { data = text ? JSON.parse(text) : null; } catch { /* порожня відповідь */ }
    return { ok: r.ok, status: r.status, data };
  };

  // 1. код запуску
  const auth = await rpc("reader_voice_auth", { p_code: String(b.code || "") });
  if (!auth.ok) {
    if (auth.data && auth.data.code === "54000") return json(429, { ok: false, error: "Забагато спроб, спробуйте за 15 хвилин" });
    return json(502, { ok: false, error: "База недоступна" });
  }
  if (auth.data !== true) return json(401, { ok: false, error: "Неправильний код" });

  if (action === "check") return json(200, { ok: true });

  if (action === "list") {
    const me = /^[a-z0-9_]{1,20}$/.test(String(b.me || "")) ? String(b.me) : null;   // хто питає: бачить спільні й свої книги
    const r = await rpc("reader_voice_list", { p_me: me });
    if (!r.ok) return json(502, { ok: false, error: "Не вдалося отримати список" });
    return json(200, { ok: true, ...r.data });
  }

  // синхронізація позиції між телефонами: ключ «профіль + id книги», доступ за тим самим кодом запуску
  if (action === "progress_get" || action === "progress_set") {
    const profile = String(b.profile || "").trim(), book = String(b.book || "");
    if (!profile || profile.length > 40 || !/^[A-Za-z0-9_-]{4,40}$/.test(book)) return json(400, { ok: false, error: "Некоректний профіль або книга" });
    if (action === "progress_get") {
      const r = await rpc("reader_progress_get", { p_profile: profile, p_book: book });
      if (!r.ok) return json(502, { ok: false, error: "Не вдалося отримати позицію" });
      return json(200, { ok: true, progress: r.data });
    }
    const pos = Number(b.pos), total = Number(b.total);
    if (!Number.isFinite(pos) || !Number.isFinite(total) || pos < 0 || total < 0 || pos > 1e6 || total > 1e6) return json(400, { ok: false, error: "Некоректна позиція" });
    const r = await rpc("reader_progress_set", { p_profile: profile, p_book: book, p_pos: pos, p_total: total });
    if (!r.ok) return json(502, { ok: false, error: "Не вдалося зберегти позицію" });
    return json(200, { ok: true });
  }

  // скасування озвучки: спершу позначаємо задачу в базі (щоб збій у GitHub не дав зайвого повідомлення в Telegram), потім зупиняємо запуск
  if (action === "cancel") {
    const id = Number(b.id);
    if (!Number.isInteger(id) || id <= 0) return json(400, { ok: false, error: "Некоректна задача" });
    if (!env.GITHUB_PAT) return json(500, { ok: false, error: "На сервері не задано токен GitHub" });
    const c = await rpc("reader_job_cancel", { p_id: id });
    if (!c.ok) return json(502, { ok: false, error: "Не вдалося скасувати задачу" });
    if (c.data !== true) return json(404, { ok: false, error: "Задача вже завершилась" });
    const repo = env.GITHUB_REPO || "abohdanovskyi/reader-app";
    const gh = { Authorization: "Bearer " + env.GITHUB_PAT, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "reader-voice" };
    let stopped = 0;
    for (const st of ["in_progress", "queued"]) {
      const r = await f(`https://api.github.com/repos/${repo}/actions/workflows/voice.yml/runs?status=${st}&per_page=5`, { headers: gh }).catch(() => null);
      const runs = r && r.ok ? ((await r.json().catch(() => null))?.workflow_runs || []) : [];
      for (const run of runs) {
        const x = await f(`https://api.github.com/repos/${repo}/actions/runs/${Number(run.id)}/cancel`, { method: "POST", headers: gh }).catch(() => null);
        if (x && x.status === 202) stopped++;
      }
    }
    return json(200, { ok: true, stopped });
  }

  const endpoint = env.R2_ENDPOINT || "", bucket = env.R2_BUCKET || "", base = (env.R2_PUBLIC_BASE || "").replace(/\/$/, "");

  if (action === "presign") {
    if (!endpoint || !bucket || !env.R2_KEY_ID || !env.R2_SECRET || !base) return json(500, { ok: false, error: "На сервері не задано ключі сховища" });
    const size = Number(b.size || 0);
    if (!(size > 1000) || size > MAX_EPUB) return json(400, { ok: false, error: "EPUB має бути від 1 КБ до 150 МБ" });
    const token = [...crypto.getRandomValues(new Uint8Array(16))].map(x => x.toString(16).padStart(2, "0")).join("");
    const key = `in/${token}/book.epub`;
    const host = new URL(endpoint).host;
    const url = await presign({ method: "PUT", host, path: `/${bucket}/${key}`, region: "auto", keyId: env.R2_KEY_ID, secret: env.R2_SECRET, expires: 900, now: now() });
    return json(200, { ok: true, put_url: url, key });
  }

  if (action === "run") {
    const key = String(b.key || "");
    if (!/^in\/[a-f0-9]{32}\/book\.epub$/.test(key)) return json(400, { ok: false, error: "Некоректний ключ файлу" });
    if (!env.GITHUB_PAT || !base) return json(500, { ok: false, error: "На сервері не задано токен GitHub" });
    const epubUrl = `${base}/${key}`;
    const head = await f(epubUrl, { method: "HEAD" }).catch(() => null);
    const len = head ? Number(head.headers.get("content-length") || 0) : 0;
    if (!head || !head.ok || len < 1000 || len > MAX_EPUB) return json(400, { ok: false, error: "EPUB не знайдено в сховищі, завантажте ще раз" });
    const title = String(b.title || "").replace(/\.epub$/i, "").slice(0, 200);
    const gender = b.gender === "male" || b.gender === "apple" ? b.gender : "female";   // apple: Леся, Мілена, Samantha (macOS)
    const forWho = b.for === undefined || b.for === null || b.for === "" ? "both" : String(b.for);   // кому призначена книга: both або ключ людини
    if (!/^[a-z0-9_]{1,20}$/.test(forWho)) return json(400, { ok: false, error: "Некоректний вибір «для кого»" });
    const job = await rpc("reader_job_create", { p_title: title, p_gender: gender, p_url: epubUrl, p_for: forWho });
    if (!job.ok) {
      if (job.data && job.data.code === "22023") return json(400, { ok: false, error: "Невідомий вибір «для кого»" });
      if (job.data && job.data.code === "55006") return json(409, { ok: false, error: "Уже йде інша озвучка, дочекайтесь її завершення" });
      return json(502, { ok: false, error: "Не вдалося створити задачу" });
    }
    const id = job.data;
    const repo = env.GITHUB_REPO || "abohdanovskyi/reader-app";
    const ref = env.GITHUB_REF || "dev";
    const gh = await f(`https://api.github.com/repos/${repo}/actions/workflows/voice.yml/dispatches`, {
      method: "POST",
      headers: {
        Authorization: "Bearer " + env.GITHUB_PAT, Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "reader-voice", "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref, inputs: { epub_url: epubUrl, gender, jobs: "12", job_id: String(id) } }),
    }).catch(() => null);
    if (!gh || gh.status !== 204) {
      await rpc("reader_job_fail", { p_id: id, p_err: `GitHub не прийняв запуск (${gh ? gh.status : "немає зв'язку"})` });
      return json(502, { ok: false, error: "GitHub не прийняв запуск озвучки" });
    }
    return json(200, { ok: true, job: id });
  }

  if (action === "delete") {
    // видалення готової книги: спершу файл у R2, потім рядок у базі (якщо файл не видалився, запис лишається)
    if (!endpoint || !bucket || !env.R2_KEY_ID || !env.R2_SECRET || !base) return json(500, { ok: false, error: "На сервері не задано ключі сховища" });
    const id = Number(b.id);
    if (!Number.isInteger(id) || id <= 0) return json(400, { ok: false, error: "Некоректна книга" });
    const l = await rpc("reader_book_link", { p_id: id });
    if (!l.ok) return json(502, { ok: false, error: "База недоступна" });
    const link = typeof l.data === "string" ? l.data : "";
    if (!link) return json(404, { ok: false, error: "Книгу не знайдено" });
    let key = "";
    if (link.startsWith(base + "/")) {
      try { key = decodeURIComponent(link.slice(base.length + 1).split(/[?#]/)[0]); } catch { key = ""; }
    }
    // у нашому сховищі видаляємо лише готові озвучки (не вхідні EPUB і не службові шляхи)
    const ownKey = key && !key.includes("..") && !key.startsWith("in/") && key.endsWith(".audiobook.zip");
    if (ownKey) {
      const url = await presign({ method: "DELETE", host: new URL(endpoint).host, path: `/${bucket}/${key}`, region: "auto", keyId: env.R2_KEY_ID, secret: env.R2_SECRET, expires: 60, now: now() });
      const d = await f(url, { method: "DELETE" }).catch(() => null);
      if (!d || !(d.ok || d.status === 404)) return json(502, { ok: false, error: "Не вдалося видалити файл зі сховища" });
    }
    const r = await rpc("reader_book_delete", { p_id: id });
    if (!r.ok) return json(502, { ok: false, error: "Файл видалено, але запис у базі лишився" });
    return json(200, { ok: true, file: ownKey });
  }

  return json(400, { ok: false, error: "Невідома дія" });
}

// @ts-ignore: Deno є лише в Supabase
if (typeof Deno !== "undefined" && (Deno as any).serve) (Deno as any).serve((req: Request) => handle(req, (Deno as any).env.toObject()));
