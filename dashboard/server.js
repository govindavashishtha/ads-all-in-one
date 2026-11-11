// Placeholder until the Next.js dashboard lands (see README.md). Serves only under basePath.
const http = require("http");
const base = process.env.NEXT_PUBLIC_BASE_PATH || "/ads-autopilot";
http
  .createServer((req, res) => {
    if (req.url === base || req.url.startsWith(base + "/")) {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(`<h1>ads-autopilot</h1><p>Dashboard placeholder (basePath ${base}).</p>`);
    } else {
      res.writeHead(404);
      res.end();
    }
  })
  .listen(3000);
