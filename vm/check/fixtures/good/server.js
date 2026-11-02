const http = require("http");
const fs = require("node:fs");
const path = require("path");
const port = process.env.PORT || 8000;
http.createServer((req, res) => {
  if (req.url === "/api/rows") { res.end(JSON.stringify([1, 2])); return; }
  fs.createReadStream(path.join(__dirname, "index.html")).pipe(res);
}).listen(port, () => console.log(`up on ${port}`));
