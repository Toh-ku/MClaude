import { test } from "node:test";
import assert from "node:assert/strict";
import { fixture } from "./helpers.js";
import { MCPRegistry } from "../src/integrations/mcp.js";
const server = `const readline=require('node:readline');readline.createInterface({input:process.stdin}).on('line',line=>{const request=JSON.parse(line);if(request.id===undefined)return;const result=request.method==='initialize'?{protocolVersion:process.argv[1],capabilities:{tools:{}},serverInfo:{name:'legacy',version:'1'}}:{tools:[]};console.log(JSON.stringify({jsonrpc:'2.0',id:request.id,result}));});`;
for (const version of ["2025-03-26", "2024-11-05"])
  test(`MCP connects to existing protocol ${version}`, async (t) => {
    const { root } = await fixture(t);
    const registry = new MCPRegistry(
      [
        {
          name: "legacy",
          command: process.execPath,
          args: ["-e", server, version],
          timeout: 2,
        },
      ],
      root,
    );
    try {
      await registry.connect(new AbortController().signal);
      assert.deepEqual(registry.definitions, []);
    } finally {
      await registry.close();
    }
  });
