// Gateway local SÓ para desenvolvimento: imita /auth/v1 (login por e-mail, senha "radar-dev")
// e repassa /rest/v1 ao PostgREST local. Nunca aponta para produção.
import http from 'node:http'
import crypto from 'node:crypto'

const SECRET = process.env.JWT_SECRET || 'dev-secret-dev-secret-dev-secret-0123456789'
const REST = process.env.REST_URL || 'http://localhost:54332'
const PORT = Number(process.env.PORT || 54331)
const USERS = {
  'luis@rcohub.dev': { id: '11111111-1111-1111-1111-111111111111', nome: 'Luis' },
  'ana@rcohub.dev': { id: '22222222-2222-2222-2222-222222222222', nome: 'Ana' },
  'dono@rcohub.dev': { id: '44444444-4444-4444-4444-444444444444', nome: 'Philip' },
}
const b64 = (o) => Buffer.from(typeof o === 'string' ? o : JSON.stringify(o)).toString('base64url')
function sign(claims) {
  const h = b64({ alg: 'HS256', typ: 'JWT' }), p = b64(claims)
  const s = crypto.createHmac('sha256', SECRET).update(`${h}.${p}`).digest('base64url')
  return `${h}.${p}.${s}`
}
function verify(t) {
  const [h, p, s] = (t || '').split('.')
  if (!s) return null
  const ok = crypto.createHmac('sha256', SECRET).update(`${h}.${p}`).digest('base64url') === s
  return ok ? JSON.parse(Buffer.from(p, 'base64url').toString()) : null
}
const userObj = (email, u) => ({ id: u.id, aud: 'authenticated', role: 'authenticated', email, app_metadata: {}, user_metadata: { nome: u.nome }, created_at: '2026-10-01T00:00:00Z' })
function session(email) {
  const u = USERS[email]
  const now = Math.floor(Date.now() / 1000)
  const access_token = sign({ sub: u.id, email, role: 'authenticated', aud: 'authenticated', iat: now, exp: now + 3600 })
  return { access_token, token_type: 'bearer', expires_in: 3600, expires_at: now + 3600, refresh_token: b64(email), user: userObj(email, u) }
}
export const SERVICE_KEY = sign({ role: 'service_role', iss: 'dev', exp: 4102444800 })
export const ANON_KEY = sign({ role: 'anon', iss: 'dev', exp: 4102444800 })

const body = (req) => new Promise((r) => { const c = []; req.on('data', (d) => c.push(d)); req.on('end', () => r(Buffer.concat(c))) })
const json = (res, code, o) => { res.writeHead(code, { 'content-type': 'application/json', 'access-control-allow-origin': '*' }); res.end(JSON.stringify(o)) }

const CORS = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': 'GET,POST,PATCH,PUT,DELETE,OPTIONS', 'access-control-expose-headers': '*' }
http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x')
  if (req.method === 'OPTIONS') { res.writeHead(204, CORS); return res.end() }
  if (url.pathname.startsWith('/auth/v1/')) {
    const raw = (await body(req)).toString()
    const data = raw ? JSON.parse(raw) : {}
    if (url.pathname === '/auth/v1/token' && url.searchParams.get('grant_type') === 'password') {
      if (USERS[data.email] && data.password === 'radar-dev') return json(res, 200, session(data.email))
      return json(res, 400, { code: 400, error_code: 'invalid_credentials', msg: 'Invalid login credentials' })
    }
    if (url.pathname === '/auth/v1/token' && url.searchParams.get('grant_type') === 'refresh_token') {
      const email = Buffer.from(data.refresh_token || '', 'base64url').toString()
      return USERS[email] ? json(res, 200, session(email)) : json(res, 400, { error_code: 'bad_refresh' })
    }
    if (url.pathname === '/auth/v1/user') {
      const c = verify((req.headers.authorization || '').replace('Bearer ', ''))
      return c ? json(res, 200, userObj(c.email, USERS[c.email])) : json(res, 401, { msg: 'invalid' })
    }
    if (url.pathname === '/auth/v1/logout') { res.writeHead(204); return res.end() }
    return json(res, 404, { msg: 'nao implementado no gateway de dev' })
  }
  if (url.pathname.startsWith('/rest/v1/')) {
    const payload = await body(req)
    const target = new URL(REST + url.pathname.replace('/rest/v1', '') + url.search)
    const headers = { ...req.headers, host: target.host }
    delete headers['content-length']
    const r = await fetch(target, { method: req.method, headers, body: ['GET', 'HEAD'].includes(req.method) ? undefined : payload })
    const out = Buffer.from(await r.arrayBuffer())
    const h = Object.fromEntries(r.headers); delete h['content-encoding']; delete h['transfer-encoding']; delete h['content-length']
    res.writeHead(r.status, { ...h, ...CORS, 'content-length': out.length }); return res.end(out)
  }
  json(res, 404, { msg: 'rota desconhecida' })
}).listen(PORT, () => console.log('fake-supabase em :' + PORT))
