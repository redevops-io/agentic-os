// The SAME journey script the shell runs, executed in Chromium via Playwright — so parity is measured, not assumed.
// usage: node chromium_journey.mjs <deployment-url> <journey-script-file>   (prints the result JSON on the last line)
import { chromium } from "playwright";
import { readFileSync } from "fs";
const [url, scriptFile] = process.argv.slice(2);
const script = readFileSync(scriptFile, "utf8");
const b = await chromium.launch();
const page = await (await b.newContext({ viewport: { width: 1280, height: 820 } })).newPage();
await page.goto(url);
const result = await page.evaluate(script);
console.log(JSON.stringify(result));
await b.close();
