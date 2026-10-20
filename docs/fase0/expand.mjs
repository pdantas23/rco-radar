// Converte um .dc.html (rascunho) em HTML estático: roda renderVals() e expande <sc-for> e {{caminho}}.
import fs from 'node:fs';
const [, , entrada, saida] = process.argv;
const src = fs.readFileSync(entrada, 'utf8');
const corpo = src.match(/<x-dc>([\s\S]*)<\/x-dc>/)[1];
const helmet = (corpo.match(/<helmet>([\s\S]*?)<\/helmet>/) || ['', ''])[1];
const tpl = corpo.replace(/<helmet>[\s\S]*?<\/helmet>/, '');
const js = src.match(/<script type="text\/x-dc"[^>]*>([\s\S]*?)<\/script>/)[1];
class DCLogic { constructor() { this.props = {}; this.state = {}; } setState() {} }
const Component = new Function('DCLogic', js + '\nreturn Component;')(DCLogic);
const vals = new Component().renderVals();

const get = (scope, path) => path.trim().split('.').reduce((o, k) => (o == null ? o : o[k]), scope);
const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
const interp = (txt, scope) => txt.replace(/\{\{\s*([\w.$]+)\s*\}\}/g, (_, p) => { const v = get(scope, p); return v == null ? '' : esc(v); });

function render(t, scope) {
  let out = '', i = 0;
  const abre = /<sc-for\s+list="\{\{\s*([\w.$]+)\s*\}\}"\s+as="(\w+)"[^>]*>/g;
  for (;;) {
    abre.lastIndex = i;
    const m = abre.exec(t);
    if (!m) return out + interp(t.slice(i), scope);
    out += interp(t.slice(i, m.index), scope);
    let prof = 1, j = abre.lastIndex;
    const tag = /<sc-for\b|<\/sc-for>/g;
    tag.lastIndex = j;
    let fim;
    while (prof) { const n = tag.exec(t); prof += n[0] === '</sc-for>' ? -1 : 1; if (!prof) fim = n.index; }
    const inner = t.slice(j, fim);
    (get(scope, m[1]) || []).forEach((item, idx) => { out += render(inner, { ...scope, [m[2]]: item, $index: idx }); });
    i = fim + '</sc-for>'.length;
  }
}
fs.writeFileSync(saida, `<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">${helmet}</head><body>${render(tpl, vals)}</body></html>`);
