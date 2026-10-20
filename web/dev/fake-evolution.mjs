// Evolution Go falsa SÓ para o dev local: aceita POST /send/text, devolve um id, e simula erros por número.
// Uso: PORT=54399 node dev/fake-evolution.mjs   (EVOLUTION_API_URL=http://localhost:54399 no .env.local do dev)
import http from "node:http";
let n = 0;
http.createServer((req, res) => {
  let b = "";
  req.on("data", (c) => (b += c));
  req.on("end", () => {
    const j = b ? JSON.parse(b) : {};
    console.log(new Date().toISOString(), req.method, req.url, JSON.stringify(j));
    res.setHeader("content-type", "application/json");
    if (req.url === "/send/text") {
      if (String(j.number).endsWith("0000")) { res.statusCode = 400; return res.end(JSON.stringify({ error: "number is not on WhatsApp" })); }
      return res.end(JSON.stringify({ data: { id: `FAKEWA${++n}` } }));
    }
    res.end("{}");
  });
}).listen(Number(process.env.PORT ?? 54399), () => console.log("fake evolution on", process.env.PORT ?? 54399));
